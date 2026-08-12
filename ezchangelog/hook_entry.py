"""The executable Claude Code runs on every Stop / SessionEnd / SessionStart.

Run as ``python -m ezchangelog.hook_entry`` (add ``statusline`` for the status
line). It reads the hook JSON on stdin and does three things, in this order:

1. resolves consent -- if sharing is off it prints nothing and stops, so off
   really does mean zero bytes and zero noise;
2. announces sharing once per session, on SessionStart, when it is on;
3. hands the upload to a detached background process and returns.

This code runs in the developer's turn loop, so the only hard requirement is
that it is fast and harmless: it always exits 0, never writes to stderr, and
never lets an exception out. A reporting tool that stutters a session is a
reporting tool that gets uninstalled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imports are deferred at runtime, see _context
    from .share import Decision
    from .store import Store

# The upload runs out of process through the same `ezcl publish` a developer
# would type, so a missing, slow or broken publisher can only ever fail in the
# background -- and whatever the hook does is exactly what a person can repeat
# by hand. `-m ezchangelog` rather than the `ezcl` script: the console script
# is not on PATH from every environment Claude Code launches the hook in.
PUBLISH_ARGV = ("-m", "ezchangelog", "publish")

DEBOUNCE_SECONDS = 5.0

PUBLISH_EVENTS = ("Stop", "SessionEnd", "SubagentStop")


def _read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _context(payload: dict[str, Any]) -> tuple["Store", str | None, str]:
    """Resolve (store, session_id, cwd) from the payload, falling back to env.

    Sibling modules are imported inside the functions that use them so that a
    broken import anywhere else in the package still exits 0 rather than
    raising before ``main`` can catch it.
    """
    from .share import current_session_id
    from .store import Store, default_store

    session_id = payload.get("session_id") or current_session_id()
    cwd = payload.get("cwd") or os.getcwd()
    return Store(default_store()), (session_id or None), str(cwd)


def _log_path(store: "Store", session_id: str) -> Path:
    logs = store.root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"publish-{session_id}.log"


def _publish_state(store: "Store", session_id: str) -> Path:
    return store.root / "publish" / f"{session_id}.json"


def _spawn_marker(store: "Store", session_id: str) -> Path:
    return store.root / "publish" / f"{session_id}.spawn"


def _recently_published(store: "Store", session_id: str) -> bool:
    """True when a publish finished, or was started, inside the debounce.

    Both files matter: ``last_published`` covers the normal case, and the spawn
    marker stops a burst of Stop events from stacking up worker processes while
    the first one is still uploading.
    """
    now = time.time()
    for path in (_publish_state(store, session_id), _spawn_marker(store, session_id)):
        try:
            if now - path.stat().st_mtime < DEBOUNCE_SECONDS:
                return True
        except OSError:
            continue
    return False


def _spawn_publish(store: "Store", session_id: str, payload: dict[str, Any]) -> None:
    marker = _spawn_marker(store, session_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")

    args = [sys.executable, *PUBLISH_ARGV, "--session", session_id]
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        # The hook is handed the exact file to publish, so the background
        # process never has to guess which transcript belongs to this session.
        args += ["--transcript", transcript]

    log = _log_path(store, session_id).open("a", encoding="utf-8")
    try:
        subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(store.root),
            start_new_session=True,  # survives the session's process group
            close_fds=True,
        )
    finally:
        log.close()


def _announce(decision: "Decision", cwd: str) -> str:
    from .share import project_name, store_url

    project = project_name(cwd)
    target = store_url(decision) or "the team store"
    return (
        f"ezup is ON for this session: the full transcript of your work in "
        f"{project} is shared with {target}, including everything typed and "
        f"everything printed by tools. Run `ezup share off` to stop. Nothing "
        f"from before now was shared."
    )


def statusline(payload: dict[str, Any]) -> str:
    """The persistent indicator, always visible once installed.

    Red dot while the transcript is being shared -- the recording-light
    metaphor is the honest one -- and a dim idle line otherwise, so a glance
    also answers "is ezup even installed?". Claude Code renders ANSI here.
    """
    dim = "\033[2m"
    red = "\033[1;31m"
    reset = "\033[0m"
    try:
        from .share import project_name, resolve, store_url

        store, session_id, cwd = _context(payload)
        decision = resolve(session_id, cwd, store)
        if not decision.sharing:
            return f"{dim}⚪ ezup off{reset}"
        target = store_url(decision) or ""
        if not target:
            # A session-level opt-in carries no store of its own; the machine
            # config is where the bytes actually go.
            from .config import load_config

            target = load_config(store, cwd).store_url
        host = target.split("//")[-1].split("/")[0] if target else "team store"
        return f"{red}🔴 ezup REC{reset} {dim}· {project_name(cwd)} → {host}{reset}"
    except Exception:
        return ""


def run(payload: dict[str, Any]) -> str | None:
    """Handle one hook fire; returns the JSON to print, or None for silence."""
    from .share import resolve

    store, session_id, cwd = _context(payload)
    decision = resolve(session_id, cwd, store)
    if not decision.sharing or session_id is None:
        # Off is silent: a tool that nags about being disabled gets ignored.
        return None

    event = str(payload.get("hook_event_name") or "")
    if event in PUBLISH_EVENTS:
        # SessionEnd is the final flush: it never fires again, so debouncing it
        # would strand the transcript's tail (the bytes after the last Stop)
        # on the developer's machine forever. Only mid-session bursts debounce.
        if event == "SessionEnd" or not _recently_published(store, session_id):
            _spawn_publish(store, session_id, payload)

    if event == "SessionStart":
        return json.dumps({"systemMessage": _announce(decision, cwd)})
    return None


def main(argv: list[str] | None = None) -> int:
    """Always returns 0. Every failure mode here is silence, never a broken turn."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = _read_payload()
        if args and args[0] == "statusline":
            line = statusline(payload)
            if line:
                sys.stdout.write(line + "\n")
        else:
            output = run(payload)
            if output:
                sys.stdout.write(output + "\n")
        sys.stdout.flush()
    except BaseException:
        # Including KeyboardInterrupt and SystemExit: whatever went wrong, the
        # developer's session must not notice.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
