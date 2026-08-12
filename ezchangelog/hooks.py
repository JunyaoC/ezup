"""Install and remove ezupdate's entries in ``~/.claude/settings.json``.

Installing is deliberately inert: it wires up the hook and the status line, and
shares nothing. Sharing starts only through :mod:`ezchangelog.share`.

settings.json belongs to the user, not to this tool, so every operation here is
surgical -- read the whole file, touch only our own entries, keep every other
key byte-for-byte, write atomically, and back the original up the first time.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import _write_json_atomic

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Every entry we write carries this substring, which is how uninstall and the
# idempotency check recognise our own work and nobody else's.
MARKER = "ezchangelog.hook_entry"

# Stop keeps an active session current, SessionEnd flushes the tail, and
# SessionStart is the only place the announcement can be printed.
HOOK_EVENTS = ("Stop", "SessionEnd", "SessionStart")

HOOK_TIMEOUT = 10


def hook_command(python: str | None = None) -> str:
    return f"{python or sys.executable} -m {MARKER}"


def statusline_command(python: str | None = None) -> str:
    """Prefer the installed console script, fall back to the interpreter.

    `ezcl` is nicer to read in a config file, but it only exists on PATH when
    the package was installed as a script -- inside a venv that the Claude Code
    process does not activate, the absolute interpreter path is what works.
    """
    if python is None:
        found = shutil.which("ezcl")
        if found:
            return f"{found} statusline"
    return f"{python or sys.executable} -m {MARKER} statusline"


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{path} is not readable JSON ({exc}); fix it first") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} does not contain a JSON object")
    return data


def _backup(path: Path) -> Path | None:
    """Copy settings.json aside once, before the first modification."""
    if not path.is_file():
        return None
    if any(path.parent.glob(f"{path.name}.bak-*")):
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = path.parent / f"{path.name}.bak-{stamp}"
    shutil.copy2(path, target)
    return target


def _entries(settings: dict[str, Any], event: str) -> list[dict[str, Any]]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    return [group for group in groups if isinstance(group, dict)]


def _is_ours(group: dict[str, Any]) -> bool:
    commands = group.get("hooks")
    if not isinstance(commands, list):
        return False
    return any(
        isinstance(item, dict) and MARKER in str(item.get("command", ""))
        for item in commands
    )


def _statusline_is_ours(settings: dict[str, Any]) -> bool:
    line = settings.get("statusLine")
    if not isinstance(line, dict):
        return False
    parts = str(line.get("command", "")).split()
    if MARKER in " ".join(parts):
        return True
    # The `ezcl statusline` form carries no marker, so match it structurally
    # rather than on a bare trailing word some other tool might also use.
    return len(parts) >= 2 and parts[-1] == "statusline" and Path(parts[-2]).name == "ezcl"


def install(settings_path: str | Path | None = None) -> dict[str, Any]:
    """Add the hook entries and the status line. Safe to run repeatedly."""
    path = Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS
    settings = _load(path)

    command = hook_command()
    added: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        if any(_is_ours(group) for group in groups if isinstance(group, dict)):
            continue
        groups.append(
            {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
        )
        hooks[event] = groups
        added.append(event)

    statusline = statusline_command()
    existing = settings.get("statusLine")
    if existing is None or _statusline_is_ours(settings):
        status_state = "unchanged" if _statusline_is_ours(settings) else "added"
        if status_state == "added":
            settings["statusLine"] = {"type": "command", "command": statusline}
    else:
        # Someone else's status line is a deliberate customisation; overwriting
        # it would be a worse surprise than a missing sharing indicator.
        status_state = "kept-existing"

    if not added and status_state != "added":
        return {
            "settings_path": str(path),
            "changed": False,
            "added_events": [],
            "statusline": status_state,
            "backup": None,
        }

    settings["hooks"] = hooks
    backup = _backup(path)
    _write_json_atomic(path, settings)
    return {
        "settings_path": str(path),
        "changed": True,
        "added_events": added,
        "statusline": status_state,
        "backup": str(backup) if backup else None,
    }


def uninstall(settings_path: str | Path | None = None) -> dict[str, Any]:
    """Remove our hook entries and our status line, and nothing else."""
    path = Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS
    settings = _load(path)

    removed: list[str] = []
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            kept = [
                group
                for group in groups
                if not (isinstance(group, dict) and _is_ours(group))
            ]
            if len(kept) == len(groups):
                continue
            removed.append(event)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)

    status_state = "absent"
    if _statusline_is_ours(settings):
        settings.pop("statusLine", None)
        status_state = "removed"
    elif "statusLine" in settings:
        status_state = "kept-existing"

    if not removed and status_state != "removed":
        return {
            "settings_path": str(path),
            "changed": False,
            "removed_events": [],
            "statusline": status_state,
        }

    _backup(path)
    _write_json_atomic(path, settings)
    return {
        "settings_path": str(path),
        "changed": True,
        "removed_events": removed,
        "statusline": status_state,
    }


def status(settings_path: str | Path | None = None) -> dict[str, Any]:
    """What is wired up right now, and where the files live."""
    path = Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS
    try:
        settings = _load(path)
        readable = True
    except RuntimeError:
        settings, readable = {}, False

    events = {
        event: any(_is_ours(group) for group in _entries(settings, event))
        for event in HOOK_EVENTS
    }
    line = settings.get("statusLine")
    return {
        "settings_path": str(path),
        "settings_exists": path.is_file(),
        "settings_readable": readable,
        "installed": all(events.values()),
        "events": events,
        "hook_command": hook_command(),
        "statusline_installed": _statusline_is_ours(settings),
        "statusline_command": (
            line.get("command") if isinstance(line, dict) else None
        ),
        "backups": sorted(str(p) for p in path.parent.glob(f"{path.name}.bak-*")),
    }
