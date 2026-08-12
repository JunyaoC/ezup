"""``ezcl`` command line interface (phase 1: mechanical extraction only)."""

from __future__ import annotations

import argparse
import base64
import inspect
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import hook_entry, hooks, picker, share
from .crypto import (
    CryptoError,
    bearer_sha256,
    generate_key,
    parse_key,
    unwrap_dk,
    wrap_dk,
)
from .collect import CollectResult, Selection, collect, normalize_roots
from .config import DEFAULT_STORE_URL, STORE_ENV, PullView, load_config, transport_for
from .console import Console
from .pipeline import STAGES as pipeline_stages_const, run_pipeline


def pipeline_stages() -> list[str]:
    return list(pipeline_stages_const)
from .publish import PublishState, publish as publish_session, readers_path
from .pull import pull as pull_sessions, pulled_sessions
from .store import Store, _write_json_atomic, default_store
from .transcripts import CLAUDE_PROJECTS_DIR, SessionFacts, scan_transcript
from .transport import HttpTransport, SessionMeta, TransportError, register_device
from .window import end_of_day, isoformat, since_to_datetime


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").replace("Z", "")[:16]


def _shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "…"


def parse_selection(text: str, count: int) -> list[int] | None:
    """Parse ``1,3,5-8`` / ``a`` / ``n`` into zero-based indices.

    Returns None when the input is unusable, so the caller can re-prompt.
    """
    text = text.strip().lower()
    if text in ("a", "all", ""):
        return list(range(count))
    if text in ("n", "none", "q"):
        return []
    chosen: list[int] = []
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        try:
            if "-" in part.lstrip("-"):
                start, _, end = part.partition("-")
                lo, hi = int(start), int(end)
                if lo > hi:
                    lo, hi = hi, lo
                span = range(lo, hi + 1)
            else:
                span = [int(part)]
        except ValueError:
            return None
        for number in span:
            if not 1 <= number <= count:
                return None
            if number - 1 not in chosen:
                chosen.append(number - 1)
    return chosen


def _columns(selections: list) -> tuple[bool, bool]:
    """Which optional columns this list needs: (synced, author).

    Decided per table rather than per row so the columns line up even when only
    some sessions carry an author or a sync state.
    """
    return (
        any(getattr(s, "synced", "") for s in selections),
        any(getattr(s, "author", "") for s in selections),
    )


def _session_row(
    index: int | None, selection, columns: tuple[bool, bool] = (False, False)
) -> str:
    facts = selection.facts
    tools = sum(facts.tool_uses.values())
    show_sync, show_author = columns
    prefix = f"{index:>3}. " if index is not None else ""
    days = f" +{len(selection.window_days) - 1}d" if len(selection.window_days) > 1 else ""
    return (
        f"{prefix}"
        f"{selection.last_active + days:<17}"
        + (f"{_shorten(getattr(selection, 'synced', '') or '-', 11)}" if show_sync else "")
        + f"{selection.match_reason:<7}"
        + (f"{_shorten(getattr(selection, 'author', '') or 'me', 13)}" if show_author else "")
        + f"{facts.user_turns:>6}  "
        f"{tools:>6}  "
        f"{_shorten(selection.display_dir, 30)}  "
        f"{_shorten(facts.title or facts.session_id[:8], 44)}"
    )


def _table_header(numbered: bool, columns: tuple[bool, bool] = (False, False)) -> str:
    show_sync, show_author = columns
    prefix = "  #  " if numbered else ""
    return (
        f"{prefix}{'ACTIVE':<17}"
        + (f"{'SYNCED':<11}" if show_sync else "")
        + f"{'WHY':<7}"
        + (f"{'WHO':<13}" if show_author else "")
        + f"{'TURNS':>6}  {'TOOLS':>6}  {'DIRECTORY':<30}  {'TITLE'}"
    )


def interactive_chooser(
    selections: list, verb: str = "collect", default_all: bool = True
) -> list:
    """Let the user pick which sessions to keep.

    Uses the full-screen checkbox picker on a terminal, and a numbered prompt
    when stdin or stdout is redirected. ``default_all`` is what a bare Enter
    means at the numbered prompt -- false for anything that leaves the machine,
    where the safe reading of "Enter" is "none of them".
    """
    if not selections:
        return []
    if sys.stdin.isatty() and sys.stdout.isatty():
        chosen = picker.pick(selections, verb)
        if chosen is picker.ABORTED:
            print(f"cancelled; nothing {verb}ed", file=sys.stderr)
            raise SystemExit(1)
        return chosen
    return numbered_chooser(selections, default_all=default_all)


def numbered_chooser(selections: list, default_all: bool = True) -> list:
    columns = _columns(selections)
    print(_table_header(numbered=True, columns=columns))
    for position, selection in enumerate(selections, start=1):
        print(_session_row(position, selection, columns))
    print()

    fallback = "Enter=all" if default_all else "Enter=none"
    while True:
        try:
            answer = input(
                f"select 1-{len(selections)} (e.g. 1,3,5-8) "
                f"[a=all, n=none, {fallback}]: "
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not answer.strip() and not default_all:
            return []
        chosen = parse_selection(answer, len(selections))
        if chosen is None:
            print(f"  ? could not read that; expected numbers 1-{len(selections)}")
            continue
        return [selections[index] for index in chosen]


def _print_result(
    result: CollectResult, store: Store, dry_run: bool, limit: int = 0
) -> None:
    header = "DRY RUN - nothing written" if dry_run else f"store: {store.root}"
    print(f"window   {isoformat(result.since)} .. {isoformat(result.until)}")
    print(f"roots    {', '.join(result.roots) or 'all projects'}")
    print(header)
    print()

    if not result.selected:
        print("no sessions matched")
    else:
        columns = _columns(result.selected)
        print(_table_header(numbered=False, columns=columns))
        shown = result.selected[:limit] if limit > 0 else result.selected
        for selection in shown:
            print(_session_row(None, selection, columns))
        hidden = len(result.selected) - len(shown)
        if hidden > 0:
            print(f"... {hidden} more (use --limit 0 to show all, or --json)")

    counts = result.manifest()["counts"]
    print()
    print(
        f"{counts['selected']} selected | "
        f"{counts['transcripts_seen']} transcripts seen "
        f"({counts['metadata_reused']} reused from index, "
        f"{counts['transcripts_parsed']} parsed) | "
        f"{counts['skipped_out_of_scope']} out of scope, "
        f"{counts['skipped_out_of_window']} out of window, "
        f"{counts['skipped_empty']} empty, "
        f"{counts['skipped_internal']} internal"
        + (
            f", {counts['deselected_by_user']} deselected"
            if counts["deselected_by_user"]
            else ""
        )
    )
    if not dry_run:
        copied = sum(1 for s in result.selected if s.copied)
        print(f"{copied} raw transcript(s) copied | manifest: runs/{result.run_id}.json")


def cmd_collect(args: argparse.Namespace) -> int:
    store = Store(Path(args.store).expanduser() if args.store else default_store())
    # `collect all` is the runner's spelling of "every project": a single bare
    # `all` positional is not a directory to resolve but a request to drop the
    # directory filter entirely, mirroring `sync all`. A real directory list --
    # even one that happens to contain other names -- is left untouched.
    directories = args.directories
    if [d.strip().lower() for d in directories] == ["all"]:
        directories = []
    roots = normalize_roots(directories)
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        print(f"warning: directory does not exist: {', '.join(missing)}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    try:
        since = since_to_datetime(args.since, now)
        # A bare --until date means "through the end of that day", not midnight.
        until = end_of_day(since_to_datetime(args.until, now)) if args.until else now
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if since > until:
        print("error: --since resolves after --until", file=sys.stderr)
        return 2

    # --yes forces the chooser-less path: every matched session proceeds, with
    # no picker and no prompt, so the runner works with no TTY. It wins over -i
    # if both are given. When --yes is absent nothing here changes -- the picker
    # is used only with -i, exactly as before.
    if args.yes:
        chooser = None
    elif args.interactive:
        chooser = interactive_chooser
    else:
        chooser = None

    result = collect(
        roots,
        since,
        until,
        store,
        recursive=not args.no_recursive,
        match_mode=args.match,
        min_turns=args.min_turns,
        min_tools=args.min_tools,
        dry_run=args.dry_run,
        refresh=args.refresh,
        include_pulled=args.include_pulled,
        chooser=chooser,
    )

    will_journal = not (args.no_journal or args.dry_run) and result.selected

    # Headless refusal: a --yes run that would have journaled but matched nothing
    # fails loudly. For an unattended runner an empty selection almost always
    # means a broken pull, not a quiet week, and silently emitting no journal
    # would hide that. An explicit --dry-run / --no-journal --yes is a report,
    # not a journaling run, so it is exempt.
    if args.yes and not result.selected and not (args.no_journal or args.dry_run):
        message = "no sessions matched the window; refusing to build an empty journal"
        if args.json:
            json.dump({"error": message, **result.manifest()}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print(f"error: {message}", file=sys.stderr)
        return 4

    # Not journaling: emit the selection exactly as before -- the manifest under
    # --json, the human table otherwise. The journal path is added after the
    # pipeline (below), so a --json caller that DOES journal gets it in the same
    # object rather than losing it to this early return.
    if not will_journal:
        if args.json:
            json.dump(result.manifest(), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            _print_result(result, store, args.dry_run, args.limit)
        return 0

    # The picker already showed the sessions; reprinting the whole table before
    # the pipeline just pushes the interesting part off screen. Under --json the
    # only stdout output is the machine-readable object emitted after the run.
    if not args.json and not args.interactive:
        _print_result(result, store, args.dry_run, args.limit)

    console = Console(verbose=not args.quiet, stages=pipeline_stages())
    # display_dir may be "~/Documents/lab/foo" in all-projects mode, so the
    # project is the leaf of the path, never its first segment.
    projects = sorted(
        {Path(s.facts.cwd).name for s in result.selected if s.facts.cwd}
    )
    shown = ", ".join(projects[:4])
    if len(projects) > 4:
        shown += f" +{len(projects) - 4}"
    home = str(Path.home())
    console.banner(
        f"{len(result.selected)} sessions  ·  "
        f"{isoformat(since)[:10]} → {isoformat(until)[:10]}",
        settings=f"{shown}   ·   store {str(store.root).replace(home, '~', 1)}",
    )
    try:
        journal = run_pipeline(
            result.selected,
            store,
            {"since": isoformat(since), "until": isoformat(until)},
            result.roots,
            console,
            dry_run=args.stop_before_model,
        )
    except KeyboardInterrupt:
        console.abort()
        print("interrupted", file=sys.stderr)
        return 130
    except BaseException:
        console.abort()  # never leave the cursor hidden
        raise
    if journal is None:
        console.abort()
        if args.json:
            json.dump({"journal": None, **result.manifest()}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 1

    # Announce where the journal landed so a script (or a person) can find it
    # without scraping the pipeline's progress output. The runner reads this.
    if args.json:
        json.dump({"journal": str(journal), **result.manifest()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"journal  {journal}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(Path(args.store).expanduser() if args.store else default_store())
    index = store.load_index()
    sources = index.get("sources", {})
    ingested = [s for s in sources.values() if s.get("stored_path")]
    runs = sorted(store.runs_dir.glob("*.json")) if store.runs_dir.is_dir() else []

    config = load_config(store, os.getcwd())
    opted_in = [
        path
        for path in sorted(share.sessions_dir(store).glob("*.share"))
        if path.read_text(encoding="utf-8").strip() == "on"
    ] if share.sessions_dir(store).is_dir() else []
    published = sorted((store.root / "publish").glob("*.json"))

    payload = {
        "store": str(store.root),
        "exists": store.root.is_dir(),
        "index_updated_at": index.get("updated_at"),
        "transcripts_indexed": len(sources),
        "transcripts_ingested": len(ingested),
        "runs": len(runs),
        "latest_run": runs[-1].stem if runs else None,
        "hook_installed": hooks.status()["installed"],
        "team_store": config.store_url or None,
        # Never the token itself, only whether one is present and from where.
        "team_token": ("set" if config.token else "missing") if config.needs_token else "n/a",
        "author": config.author,
        "sessions_sharing": len(opted_in),
        "sessions_published": len(published),
        "sessions_pulled": len(pulled_sessions(store)),
    }
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for key, value in payload.items():
            print(f"{key:<22}{value}")
    return 0


# -- sharing plumbing ---------------------------------------------------------


def _store_for(args: argparse.Namespace) -> Store:
    return Store(Path(args.store).expanduser() if args.store else default_store())


def _human(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _emit(payload: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    for line in lines:
        print(line)


def _current_session(args: argparse.Namespace) -> str | None:
    """The session to act on: the one named, else the one we are running in."""
    named = getattr(args, "session", None)
    return str(named) if named else share.current_session_id()


def _find_transcript(session_id: str, store: Store) -> Path | None:
    """Locate a session's transcript: live first, then the store's copy.

    ``~/.claude/projects`` is authoritative because it is the file Claude Code
    is still appending to; the store copy only helps for a session whose
    original has since been deleted.
    """
    matches = sorted(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if matches:
        return matches[0]
    copies = sorted(store.raw_dir.glob(f"*/{session_id}.jsonl"))
    if copies:
        return copies[0]
    pulled = sorted((store.root / "pulled").glob(f"*/{session_id}.jsonl"))
    return pulled[0] if pulled else None


def _facts(path: Path) -> SessionFacts:
    try:
        return scan_transcript(path)
    except OSError:
        return SessionFacts(
            session_id=path.stem, source_path=str(path), project_slug=path.parent.name
        )


def _meta_for(session_id: str, facts: SessionFacts, author: str, cwd: str) -> SessionMeta:
    return SessionMeta(
        session=session_id,
        author=author,
        project=share.project_name(facts.cwd or cwd),
        branch=facts.git_branches[0] if facts.git_branches else "",
        cwd=facts.cwd or cwd,
        first_ts=facts.first_timestamp or "",
        last_ts=facts.last_timestamp or "",
        title=facts.title or "",
        level="raw",
    )


def _sync_refusal(store: Store, session_id: str, cwd: str) -> str:
    """Why `sync` may not share this session, or "" when it may.

    `sync` is itself a consent step, so the states that mean "nobody has decided
    yet" -- a repo that says ``ask``, or no policy at all -- are exactly what it
    exists to offer. A decision that *was* already made is a different thing: an
    explicit `ezup share off`, a repo-level ``never``, or a resolution that
    failed outright must not be undone by one keystroke in a picker.
    """
    decision = share.resolve(session_id, cwd, store)
    if decision.sharing:
        return ""
    if decision.source in ("session", "error"):
        return decision.reason
    policy = share.effective_policy(cwd)
    return decision.reason if policy is not None and policy.mode == "never" else ""


def _sync_label(store: Store, selection: Selection) -> str:
    """The SYNCED column: how much of this transcript has already left."""
    path = Path(selection.stored_path or selection.facts.source_path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    state = PublishState.load(store, selection.facts.session_id)
    if state.offset <= 0:
        return "never"
    if state.offset >= size:
        return "up to date"
    return f"+{_human(size - state.offset)}"


# -- hook ---------------------------------------------------------------------


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        if args.action == "install":
            info = hooks.install(args.settings)
        elif args.action == "uninstall":
            info = hooks.uninstall(args.settings)
        else:
            info = hooks.status(args.settings)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    lines = [f"{key:<22}{value}" for key, value in info.items()]
    if args.action == "install":
        # Say this every time: an installer that silently starts uploading is
        # exactly the thing this feature is built not to be.
        lines.append("")
        lines.append(
            "installed, and sharing is still OFF. Nothing leaves this machine "
            "until you run `ezup share on`."
        )
    _emit(info, args.json, lines)
    return 0


# -- share --------------------------------------------------------------------


def _share_status(store: Store, session_id: str | None, cwd: str, as_json: bool) -> int:
    decision = share.resolve(session_id, cwd, store)
    config = load_config(store, cwd)
    state = PublishState.load(store, session_id) if session_id else PublishState("")
    payload = {
        "session": session_id,
        "sharing": decision.sharing,
        "state": decision.state,
        "source": decision.source,
        "reason": decision.reason,
        "repo": config.repo,
        "store": config.store_url,
        "token": "set" if config.token else ("missing" if config.needs_token else "n/a"),
        "author": config.author,
        "published_bytes": state.offset,
        "last_published": state.last_published,
    }
    lines = [
        f"session   {session_id or 'unknown (not inside a Claude Code session)'}",
        f"sharing   {decision.state}",
        f"why       {decision.reason}",
        *config.describe(),
        # offset is where publishing has reached in the document; what actually
        # left the machine is only the part above the consent watermark.
        f"published {_human(max(0, state.offset - state.start_offset))}"
        + (f" (last {state.last_published})" if state.last_published else ""),
    ]
    _emit(payload, as_json, lines)
    return 0


def _ack_policy(
    store: Store, session_id: str | None, cwd: str, as_json: bool, say: Any
) -> int:
    """Accept the committed ``always`` policy governing ``cwd`` -- and only that.

    What is acknowledged is a specific policy document, quoted back in full
    before it is accepted, not "this directory". Anything else -- no policy, a
    policy that is not ``always`` -- is refused out loud, because an ack of a
    repo that currently asks for nothing is a blank cheque for whatever it asks
    for after the next `git pull`.
    """
    policy = share.effective_policy(cwd)
    if policy is None:
        print(
            f"error: no .ez/config.json at or above {cwd} declares a \"share\" "
            f"policy, so there is nothing to acknowledge. `share ack` accepts a "
            f"repo's committed policy; to share just this session run "
            f"`ezup share on`.",
            file=sys.stderr,
        )
        return 2
    if policy.mode != "always":
        print(
            f"error: {policy.where} says \"share\": \"{policy.mode}\", which "
            f"needs no acknowledgement"
            + (
                " -- sessions under it are never shared."
                if policy.mode == "never"
                else " -- run `ezup share on` per session instead."
            ),
            file=sys.stderr,
        )
        return 2

    # Quote the policy before accepting it: consent to a hash nobody read is
    # not consent. `store` is called out by name because it is the field that
    # decides where the bytes actually land.
    say(f"policy   {policy.where}")
    say(f"  share  always — every session under {policy.repo} is uploaded")
    say(f"  store  {policy.store or 'not set here (falls back to this machine)'}")
    for key, value in sorted(policy.config.items()):
        if key not in ("share", "store"):
            say(f"  {key:<6} {value}")

    try:
        path = share.acknowledge(policy.repo, store)
    except share.ShareRefused as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    say(
        f"acknowledged on this machine ({path}). This accepts the file exactly "
        f"as printed above: if any of it changes, sharing reverts to off until "
        f"you run `ezup share ack` again."
    )
    return _share_status(store, session_id, cwd, as_json)


def _watermark_session(store: Store, session_id: str) -> int | None:
    """Record that only bytes written *after* now may ever be published.

    A fresh :class:`PublishState` starts at offset 0, so the first upload after
    an opt-in would carry the whole transcript -- including the 40 minutes of
    customer data the developer debugged before deciding to share. Seeding
    ``start_offset`` with the file's current size makes "nothing from before now
    is sent" a fact rather than a claim.

    Returns the watermark, or None when the session has already published (the
    resume offset already covers it) or has no transcript on disk yet.
    """
    state = PublishState.load(store, session_id)
    if state.published:
        return None
    path = _find_transcript(session_id, store)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    state.start_offset = size
    state.save(store)
    return size


def cmd_share(args: argparse.Namespace) -> int:
    store = _store_for(args)
    cwd = os.getcwd()
    session_id = _current_session(args)

    def say(message: str) -> None:
        # --json must stay machine-readable, so the prose goes to stderr there
        # rather than being dropped: a person watching still sees what changed.
        print(message, file=sys.stderr if args.json else sys.stdout)

    if args.action == "status":
        return _share_status(store, session_id, cwd, args.json)

    if args.action == "ack":
        return _ack_policy(store, session_id, cwd, args.json, say)

    if session_id is None:
        print(
            f"error: no session id; run this inside a Claude Code session or "
            f"pass --session (${share.SESSION_ENV} is unset)",
            file=sys.stderr,
        )
        return 2

    if args.action == "clear":
        cleared = share.clear_session(store, session_id)
        say(
            f"{'cleared' if cleared else 'nothing to clear for'} {session_id}; "
            f"the repo policy applies again"
        )
        return _share_status(store, session_id, cwd, args.json)

    try:
        share.set_session(session_id, args.action == "on", store, cwd=cwd)
    except share.ShareRefused as error:
        print(f"error: {error}", file=sys.stderr)
        return 3

    if args.action == "on":
        config = load_config(store, cwd)
        watermark = _watermark_session(store, session_id)
        say(
            f"sharing ON for session {session_id}: from now on, the full "
            f"transcript of this session is uploaded to "
            f"{config.store_url or 'the store (not configured yet)'}, including "
            f"everything you type and everything tools print. Run "
            f"`ezup share off` to stop."
        )
        if watermark:
            say(
                f"the {_human(watermark)} of this session recorded before now "
                f"stays on this machine: publishing starts at byte {watermark}. "
                f"Run `ezup sync` if you do want the earlier part shared too."
            )
        elif PublishState.load(store, session_id).published:
            say(
                "this session has published before, so sharing resumes where it "
                "left off; `ezup unpublish` removes what is already up there."
            )
        else:
            say("nothing was recorded before now, so nothing earlier exists to send.")
    else:
        say(
            f"sharing OFF for session {session_id}: no further bytes leave this "
            f"machine. Anything already published stays until you run "
            f"`ezup unpublish --session {session_id}`."
        )
    return _share_status(store, session_id, cwd, args.json)


# -- publish ------------------------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> int:
    store = _store_for(args)
    session_id = _current_session(args)
    if session_id is None:
        print(
            f"error: no session id; pass --session or run inside a Claude Code "
            f"session (${share.SESSION_ENV} is unset)",
            file=sys.stderr,
        )
        return 2

    path = Path(args.transcript).expanduser() if args.transcript else _find_transcript(
        session_id, store
    )
    if path is None or not path.is_file():
        print(f"error: no transcript found for session {session_id}", file=sys.stderr)
        return 2

    facts = _facts(path)
    # The hook runs this from the store directory, so the process cwd says
    # nothing about which repo the work happened in. The transcript does.
    cwd = facts.cwd or os.getcwd()

    decision = share.resolve(session_id, cwd, store)
    if not decision.sharing and not args.dry_run:
        print(f"not sharing: {decision.reason}", file=sys.stderr)
        return 3

    config = load_config(store, cwd)
    try:
        transport = transport_for(config)
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        report = publish_session(
            session_id,
            path,
            transport,
            store,
            _meta_for(session_id, facts, config.author, cwd),
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, TransportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        if not decision.sharing:
            print(f"sharing is off ({decision.source}); this is a preview only")
        print(report.describe())
    return 0


def cmd_unpublish(args: argparse.Namespace) -> int:
    store = _store_for(args)
    session_id = args.session
    path = _find_transcript(session_id, store)
    cwd = (_facts(path).cwd if path else None) or os.getcwd()
    config = load_config(store, cwd)
    try:
        transport = transport_for(config)
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to delete without --yes", file=sys.stderr)
            return 2
        answer = input(
            f"delete every published byte of session {session_id} from "
            f"{transport.describe()}? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1

    try:
        transport.delete_session(session_id)
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    # Forget the offsets too, or the next publish would resume mid-file into a
    # session that no longer exists on the server.
    PublishState.path_for(store, session_id).unlink(missing_ok=True)
    # And stop the hook from immediately re-uploading what was just deleted.
    share.set_session(session_id, False, store, cwd=cwd)
    print(
        f"deleted session {session_id} from {transport.describe()}; sharing for "
        f"it is now off"
    )
    return 0


# -- sync ---------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    store = _store_for(args)
    now = datetime.now(timezone.utc)
    if args.window.strip().lower() == "all":
        # Everything ever recorded. Safe to offer because sync never
        # pre-ticks anything: "all" widens the LIST, not the selection.
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    else:
        try:
            since = since_to_datetime(args.window, now)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    result = collect(
        normalize_roots(args.directories),
        since,
        now,
        store,
        min_turns=args.min_turns,
        min_tools=args.min_tools,
        dry_run=True,  # sync shares transcripts; it never ingests or journals
    )
    if not result.selected:
        print(f"no sessions since {isoformat(since)[:16]}")
        return 0

    blocked: dict[str, str] = {}
    for selection in result.selected:
        session_id = selection.facts.session_id
        refusal = _sync_refusal(store, session_id, selection.facts.cwd or ".")
        if refusal:
            blocked[session_id] = refusal
        selection.synced = "BLOCKED" if refusal else _sync_label(store, selection)

    print(f"window   {isoformat(since)[:16]} .. now")
    print(f"store    {store.root}")
    print(
        "tick the sessions whose full transcript may leave this machine; "
        "nothing is ticked to begin with"
    )
    if blocked:
        # Listed before the picker, not just marked in it: [a] ticks every row
        # in one keystroke, so the user has to be able to see what that keystroke
        # cannot include.
        print(
            f"{len(blocked)} session(s) are BLOCKED and will not be shared even "
            f"if ticked:"
        )
        for session_id, refusal in blocked.items():
            print(f"  {session_id[:8]}  {refusal}")
    print()
    chosen = interactive_chooser(result.selected, verb="share", default_all=False)
    if not chosen:
        print("nothing shared")
        return 0

    failures = 0
    transports: dict[tuple[str, str, str], Any] = {}
    for selection in chosen:
        session_id = selection.facts.session_id
        cwd = selection.facts.cwd or os.getcwd()
        # Re-checked here rather than trusting the label: between drawing the
        # table and confirming it, the only thing that must be true is that this
        # session is still allowed to leave.
        refusal = _sync_refusal(store, session_id, cwd)
        if refusal:
            print(f"{session_id[:8]}  refused: {refusal}")
            failures += 1
            continue

        config = load_config(store, cwd)
        # Each session resolves its own destination -- two repos in one window
        # can publish to two different stores, or with two different tokens.
        key = (config.store_url, config.token, config.author)
        try:
            if key not in transports:
                transports[key] = transport_for(config)
            transport = transports[key]
        except TransportError as error:
            print(f"{session_id[:8]}  error: {error}", file=sys.stderr)
            failures += 1
            continue

        path = Path(selection.stored_path or selection.facts.source_path)
        try:
            report = publish_session(
                session_id,
                path,
                transport,
                store,
                _meta_for(session_id, selection.facts, config.author, cwd),
                dry_run=args.dry_run,
            )
        except (FileNotFoundError, TransportError) as error:
            print(f"{session_id[:8]}  error: {error}", file=sys.stderr)
            failures += 1
            continue

        verb = "would send" if args.dry_run else "sent"
        print(
            f"{session_id[:8]}  {verb} {_human(report.bytes_sent)} in "
            f"{len(report.chunks)} chunk(s) to {report.destination}"
        )
        # The scan is deliberately trigger-happy (see publish.secret_scan), so
        # a session's worth of findings would bury the summary. `ezcl publish
        # --dry-run --session ...` prints all of them.
        for warning in report.warnings[:3]:
            print(f"          WARNING possible secret: {warning}")
        if len(report.warnings) > 3:
            print(
                f"          ... {len(report.warnings) - 3} more possible secrets; "
                f"see `ezcl publish --dry-run --session {session_id}`"
            )
        if args.enable and not args.dry_run:
            try:
                share.set_session(session_id, True, store, cwd=cwd)
            except share.ShareRefused as error:
                print(f"          {error}", file=sys.stderr)

    if args.enable and not args.dry_run:
        print("these sessions stay shared: the hook will keep them current")
    return 1 if failures else 0


# -- reader tokens ------------------------------------------------------------


def _read_json_file(path: Path) -> dict[str, Any]:
    """Best-effort JSON read; a missing or corrupt file is "no data", not fatal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _readers(store: Store) -> list[dict[str, Any]]:
    data = _read_json_file(readers_path(store))
    rows = data.get("readers")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _write_readers(store: Store, rows: list[dict[str, Any]]) -> None:
    """Persist readers.json machine-private (0600): it carries each reader's
    K_enc, which lets this device wrap DKs *to* the reader forever. It never
    authenticates *as* the reader (K_auth is HKDF-independent), but it is still
    key material and must not be world-readable or land in a repo."""
    path = readers_path(store)
    _write_json_atomic(path, {"version": 1, "readers": rows})
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _grant_history(
    transport: HttpTransport,
    reader_id: str,
    reader_enc_key: bytes,
    say: Any,
) -> tuple[int, list[tuple[str, str]]]:
    """Re-wrap every session's data key for a freshly minted reader.

    This is the O(sessions) backfill the contract describes (D8 / 6.3 step 4).
    Every session's DK is already wrapped for this device itself and stored
    server-side, so the device recovers each DK from its own self-wrap and
    re-wraps it under the reader's K_enc -- no need to have kept any DK locally.

    Returns ``(granted, skipped)`` where ``skipped`` names the sessions whose
    self-wrap could not be opened (reported, never fatal).
    """
    # Bulk fetch of THIS device's own wraps. The GET response also carries the
    # caller's recipient_id, which HttpTransport caches into device_id -- so
    # this call is what teaches us the own-device id the self-wrap AAD is bound
    # to when the config did not already carry it.
    self_wraps = transport.get_wrapped_keys()
    own_id = transport.device_id
    if not own_id:
        # No self-wraps and no configured id: nothing to unwrap against, so the
        # backfill is a no-op rather than an error (a device that has published
        # nothing encrypted has no history to grant).
        return 0, []

    device_key = transport.key_set  # the configured ezu_ device key (guaranteed by caller)
    batch: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    for wrap in self_wraps:
        session = str(wrap.get("session") or "")
        try:
            enc_gen = int(wrap.get("enc_gen") or 0)
            blob = base64.b64decode(str(wrap.get("wrap") or ""))
            dk = unwrap_dk(device_key.enc_key, session, own_id, enc_gen, blob)
        except (CryptoError, ValueError, TypeError) as exc:
            # A self-wrap we cannot open cannot be re-granted; skip it loudly so
            # the developer knows that one session will not be readable by the
            # new reader, and move on rather than aborting the whole grant.
            skipped.append((session, str(exc)))
            continue
        rewrapped = wrap_dk(reader_enc_key, session, reader_id, enc_gen, dk)
        batch.append(
            {
                "session": session,
                "recipient_id": reader_id,
                "enc_gen": enc_gen,
                "wrap": base64.b64encode(rewrapped).decode("ascii"),
            }
        )

    if not batch:
        return 0, skipped
    say(f"granting {len(batch)} session(s) to the new reader...")
    granted = transport.put_wrapped_keys(batch)  # batched <= 500 inside the transport
    return granted, skipped


def _mint_reader(
    args: argparse.Namespace,
    store: Store,
    transport: HttpTransport,
    say: Any,
) -> int:
    """Client-side reader mint + history backfill (contract 6.3).

    The reader secret is generated here and never sent; the server stores only
    sha256 of its derived bearer, so it can authenticate the reader later
    without ever being able to become it.
    """
    key_set = transport.key_set
    if key_set is None or key_set.kind != "device":
        # Minting a reader and wrapping DKs for it both require this machine's
        # own device key: the wire call needs a device bearer, and the backfill
        # needs the device's K_enc to open its self-wraps. A reader key or a raw
        # ezw_ bearer can do neither.
        print(
            "error: `ezup token mint` needs this machine's device key (the "
            "pasted ezu_ key) configured as the store token; a reader key or a "
            "raw bearer cannot mint readers or wrap data keys for them",
            file=sys.stderr,
        )
        return 2

    # 1. Generate the reader's key on the client. Printed once at the end.
    pasted, reader_keys = generate_key("reader")

    # 2. Register only sha256(bearer) with the server; get back the reader's
    #    recipient device id (used as the wrap AAD recipient from here on).
    try:
        response = transport.mint_reader(args.name, bearer_sha256(pasted))
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    reader_id = str(response.get("id") or "")
    if not reader_id:
        print("error: the store did not return a reader id", file=sys.stderr)
        return 1

    # 3. Record the grant locally so future publishes keep wrapping for it.
    rows = _readers(store)
    rows.append(
        {
            "reader_id": reader_id,
            "name": args.name,
            "keyid": reader_keys.keyid,
            "enc_key": reader_keys.enc_key.hex(),
            "created_at": isoformat(datetime.now(timezone.utc)),
        }
    )
    _write_readers(store, rows)

    # 4. History backfill: re-wrap every existing session's DK for the reader.
    try:
        granted, skipped = _grant_history(
            transport, reader_id, reader_keys.enc_key, say
        )
    except TransportError as error:
        # The reader exists and readers.json is written, so future sessions are
        # already covered; only the backfill of past sessions failed. Say so
        # precisely rather than pretending the mint failed.
        print(
            f"error: reader minted, but granting existing sessions failed: "
            f"{error}. Re-run `ezup token mint` is not needed; a fresh publish "
            f"of each session will grant it, or retry when the store is reachable.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        json.dump(
            {
                "reader_id": reader_id,
                "name": args.name,
                "keyid": reader_keys.keyid,
                "token": pasted,
                "granted": granted,
                "skipped": [s for s, _ in skipped],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    # The pasted key is shown exactly once and stored nowhere on this machine.
    print(f"reader token for {args.name} (keyid {reader_keys.keyid[:8]}):")
    print()
    print(f"    {pasted}")
    print()
    print(
        "copy it now -- it is shown ONCE and is stored nowhere on this machine. "
        "Send it to the reader over a private channel."
    )
    print(
        f"they can read every session you have shared "
        f"({granted} granted just now) and every session you share from now on, "
        f"until you run:"
    )
    print(f"    ezup token revoke {args.name}")
    if skipped:
        print()
        print(
            f"WARNING {len(skipped)} session(s) could not be granted (their "
            f"self-wrap would not open); they will not be readable by this reader:"
        )
        for session, reason in skipped[:5]:
            print(f"  {session[:16]:<18}{reason}")
        if len(skipped) > 5:
            print(f"  ... {len(skipped) - 5} more")
    return 0


# -- pull ---------------------------------------------------------------------


def _admin_token(args: argparse.Namespace) -> str | None:
    """The admin token, from --admin-token, $EZUP_ADMIN_TOKEN, or the admin file."""
    if getattr(args, "admin_token", None):
        return str(args.admin_token).strip()
    env = os.environ.get("EZUP_ADMIN_TOKEN", "").strip()
    if env:
        return env
    path = Path.home() / ".ezchangelog" / "admin-token"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return None


def cmd_login(args: argparse.Namespace) -> int:
    """Write this machine's credential in one step.

    For the common case: someone was handed a device token + id (from
    ``ezup device mint``) and just wants this machine configured. No hand-edited
    JSON, no admin token. The store defaults to the team store.
    """
    store = _store_for(args)
    token = args.token.strip()
    try:
        keyset = parse_key(token)
    except CryptoError as error:
        print(f"error: that does not look like a valid key: {error}", file=sys.stderr)
        return 2
    if keyset.kind != "device":
        print("error: `ezup login` takes a device key (ezu_...). A reader key "
              "(ezr_...) goes in the keyring: `ezup keyring add`.", file=sys.stderr)
        return 2
    if not args.device_id and not args.no_device_id:
        print("error: a device token needs its device_id too (the uuid printed "
              "beside it). Pass it as the second argument, or --no-device-id if "
              "you only need read/publish without granting readers.",
              file=sys.stderr)
        return 2

    existing = load_config(store, os.getcwd())
    if existing.token and not args.force:
        print(f"error: this machine already has a device token (author "
              f"{existing.author or '?'}). Use --force to replace it (this "
              f"orphans the old device's sessions).", file=sys.stderr)
        return 2

    cfg_path = store.root / "config.json"
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    cfg["store"] = args.store or cfg.get("store") or DEFAULT_STORE_URL
    cfg["token"] = token
    if args.device_id:
        cfg["device_id"] = args.device_id.strip()
    if args.author:
        cfg["author"] = args.author
    store.root.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    cfg_path.chmod(0o600)
    _emit(
        {"store": cfg["store"], "device_id": cfg.get("device_id"), "configured": True},
        args.json,
        [
            f"logged in to {cfg['store']}",
            f"config written to {cfg_path} (chmod 600)",
            "you can now share (ezup hook install; /ezup on) and, with a "
            "device_id, mint reader keys (ezup token mint).",
        ],
    )
    return 0


def cmd_device(args: argparse.Namespace) -> int:
    """Enrol a device (admin-gated). The device SECRET is generated here; the
    server only ever receives its hash, so it can never publish or read as the
    device it registers."""
    store = _store_for(args)
    config = load_config(store, os.getcwd())
    base = config.store_url
    if not base:
        print(f"error: no store configured; set ${STORE_ENV} or a store in "
              f"<store>/config.json", file=sys.stderr)
        return 2
    # No admin token needed when the store allows open enrollment; if it is
    # admin-gated the server returns 401/403 and register_device's error already
    # says to supply one. So we send whatever we have (possibly empty).
    admin = _admin_token(args) or ""

    # Enrolling writes config.json. If this machine is already a device,
    # overwriting it orphans every session that device owns (only the owning
    # device may manage them), so refuse unless --force. Learned the hard way.
    if args.token_command == "enroll" and config.token and not args.force:
        print(
            f"error: this machine is already enrolled as a device "
            f"(author {config.author or '?'}). Enrolling again would orphan its "
            f"sessions. Use `ezup device mint` to enrol someone else, or "
            f"--force to replace this device anyway.",
            file=sys.stderr,
        )
        return 2

    email = args.email or f"{args.name}@ezup.local"
    pasted, keyset = generate_key("device")
    try:
        resp = register_device(base, admin, args.name, email, bearer_sha256(pasted))
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    device_id = str(resp.get("id"))

    if args.token_command == "enroll":
        # This machine becomes the device: write token + id into config.
        cfg_path = store.root / "config.json"
        cfg = {}
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
        cfg.update({"store": base, "token": pasted, "device_id": device_id})
        cfg.setdefault("author", args.name)
        store.root.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        cfg_path.chmod(0o600)
        _emit(
            {"device_id": device_id, "enrolled": True},
            args.json,
            [
                f"enrolled this machine as device {device_id}",
                f"config written to {cfg_path} (chmod 600)",
                "you can now share your own sessions (ezup hook install; /ezup on)"
                " and mint reader keys (ezup token mint).",
            ],
        )
        return 0

    # mint: register a device for SOMEONE ELSE; hand them the key + id.
    _emit(
        {"device_id": device_id, "token": pasted},
        args.json,
        [
            f"device minted for {args.name!r} -- give them BOTH, shown once:",
            "",
            f"  token      {pasted}",
            f"  device_id  {device_id}",
            "",
            f"they put these in their ~/.ezchangelog/config.json as \"token\" and "
            f'"device_id" (store {base}). Treat the token like a password.',
        ],
    )
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """Reader-token management: a dev grants an operator read access."""
    store = _store_for(args)
    config = load_config(store, os.getcwd())

    if args.token_command == "show":
        # Needs no transport: the token lives in this machine's own config.
        # The web viewer's "login" is this token, so a developer needs a way
        # to retrieve their own. Reader tokens stay unrecoverable -- only
        # their sha256 exists anywhere -- this shows the DEVICE token.
        if not config.token:
            print("no token configured; see the README's enrolment steps", file=sys.stderr)
            return 1
        lines = [
            f"store  {config.store_url}",
            f"token  {config.token}",
            "",
            "this is this machine's device token — it is the login for the",
            f"web viewer at {config.store_url}. Treat it like a password.",
        ]
        decision = share.resolve(share.current_session_id(), os.getcwd(), store)
        if decision.sharing:
            lines.append(
                "WARNING: this session is being SHARED right now; the token "
                "just printed is going into the shared transcript. Consider "
                "`ezup share off` first, then run this again."
            )
        _emit({"store": config.store_url, "token": config.token}, args.json, lines)
        return 0

    try:
        transport = transport_for(config)
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not isinstance(transport, HttpTransport):
        print(
            "error: reader tokens need a worker store; this store is a local "
            "directory, which has no auth to grant",
            file=sys.stderr,
        )
        return 2

    try:
        if args.token_command == "mint":
            # --json keeps stdout machine-readable, so progress prose goes to
            # stderr there rather than being dropped.
            def say(message: str) -> None:
                print(message, file=sys.stderr if args.json else sys.stdout)

            return _mint_reader(args, store, transport, say)
        if args.token_command == "list":
            rows = transport.list_readers()
            lines = [f"{len(rows)} reader token(s) minted by this device"]
            for row in rows:
                state = "revoked" if row.get("revoked_at") else "active"
                lines.append(
                    f"  {row.get('id','?'):<38}{state:<9}"
                    f"{row.get('name','')}  ({str(row.get('created_at',''))[:10]})"
                )
            _emit({"tokens": rows}, args.json, lines)
            return 0
        # revoke -- by id, by name, or bare when only one token is active.
        active = [r for r in transport.list_readers() if not r.get("revoked_at")]
        wanted = (args.id or "").strip()
        if not wanted:
            if len(active) == 1:
                target = active[0]
            elif not active:
                print("no active reader tokens to revoke", file=sys.stderr)
                return 1
            else:
                print(
                    "several active tokens; name one:  "
                    + "  ".join(f"{r.get('name','?')} ({r.get('id','?')[:8]})" for r in active),
                    file=sys.stderr,
                )
                return 2
        else:
            matches = [
                r for r in active
                if r.get("id") == wanted
                or str(r.get("id", "")).startswith(wanted)
                or r.get("name") == wanted
            ]
            if not matches:
                print(f"no active token matches {wanted!r}; see `ezup token list`", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(f"{wanted!r} is ambiguous; use the full id from `ezup token list`", file=sys.stderr)
                return 2
            target = matches[0]
        gone = transport.revoke_reader(str(target.get("id")))
        # Drop the local grant too, so future publishes stop wrapping DKs for a
        # reader the server will now refuse anyway. Match on the revoked id.
        target_id = str(target.get("id"))
        rows = _readers(store)
        remaining = [r for r in rows if str(r.get("reader_id")) != target_id]
        if len(remaining) != len(rows):
            _write_readers(store, remaining)
        _emit(
            gone,
            args.json,
            [
                f"revoked {target.get('name','?')} ({target.get('id','?')}); that "
                f"token stops working now. Already-published DKs it holds stay "
                f"decryptable until each session's generation rotates (contract Q1)."
            ],
        )
        return 0
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


# -- keyring ------------------------------------------------------------------
# The reader keyring lives in ezchangelog/keyring.py, owned by another module.
# It is imported lazily and every access is defensive, so a build without it
# (or an older one) keeps `pull` working on the single configured device token
# and simply reports keyring commands as unavailable -- never an import-time
# failure that would take the whole CLI down with it.


def _keyring_module() -> Any | None:
    try:
        from . import keyring as module  # type: ignore
    except Exception:
        return None
    return module


def _load_keyring(store: Store) -> Any | None:
    module = _keyring_module()
    if module is None:
        return None
    loader = getattr(module, "load_keyring", None)
    if not callable(loader):
        return None
    try:
        return loader(store)
    except Exception:
        return None


def _entry_field(entry: Any, name: str, default: str = "") -> str:
    """One field of a keyring entry, tolerating both dict and dataclass shapes."""
    if isinstance(entry, dict):
        return str(entry.get(name, default) or default)
    return str(getattr(entry, name, default) or default)


def _keyring_entries(keyring: Any) -> list[Any]:
    """The reader entries a Keyring holds.

    The stored schema is pinned by the contract (6.5: token/keyid/reader_id/
    label/store), so the pull loop reads entries directly rather than depending
    on method names that may still be settling. Tries the likely container
    attributes in turn.
    """
    if keyring is None:
        return []
    for attr in ("entries", "keys", "readers"):
        value = getattr(keyring, attr, None)
        if isinstance(value, list):
            return value
    if isinstance(keyring, list):
        return keyring
    return []


def _call_pull(
    view: Any,
    store: Store,
    *,
    since: str | None,
    authors: list[str] | None,
    keyid: str | None,
    allow_legacy: bool,
) -> Any:
    """Invoke pull, passing only the keyword arguments this pull build accepts.

    ``keyid`` (per-key cursor scoping) and ``allow_legacy`` (show unverified
    legacy plaintext) are forwarded only when the installed ``pull`` declares
    them, so this CLI stays compatible with a pull module that has not yet
    grown either parameter.
    """
    params = inspect.signature(pull_sessions).parameters
    kwargs: dict[str, Any] = {"since": since, "authors": authors}
    if "keyid" in params:
        kwargs["keyid"] = keyid
    if "allow_legacy" in params:
        kwargs["allow_legacy"] = allow_legacy
    return pull_sessions(view, store, **kwargs)


def _resolve_since(args: argparse.Namespace) -> str | None | bool:
    """The --since instant, or False on a parse error (already reported)."""
    if not args.since:
        return None
    try:
        return isoformat(since_to_datetime(args.since, datetime.now(timezone.utc)))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return False


def _pull_keyring(
    args: argparse.Namespace, store: Store, entries: list[Any], allow_legacy: bool
) -> int:
    """Run the existing pull once per keyring entry, against each entry's store.

    Cursors are scoped per key (contract 6.5): one revoked key reports its own
    error and holds only its own cursor, while the other keys complete. The
    union of every key's sessions lands under ``<store>/pulled/<author>/``.
    """
    since = _resolve_since(args)
    if since is False:
        return 2

    totals = {"sessions_new": 0, "sessions_updated": 0, "chunks": 0, "bytes": 0}
    all_errors: list[str] = []
    per_key: list[dict[str, Any]] = []

    for entry in entries:
        token = _entry_field(entry, "token")
        keyid = _entry_field(entry, "keyid")
        label = _entry_field(entry, "label") or keyid[:8] or "reader"
        store_url = _entry_field(entry, "store")
        reader_id = _entry_field(entry, "reader_id")
        tag = f"{label} ({keyid[:8]})" if keyid else label

        if not token or not store_url:
            all_errors.append(f"{tag}: keyring entry is missing its token or store")
            per_key.append({"key": label, "keyid": keyid, "error": "incomplete entry"})
            continue

        try:
            transport = HttpTransport(store_url, token, device_id=reader_id)
        except TransportError as error:
            all_errors.append(f"{tag}: {error}")
            per_key.append({"key": label, "keyid": keyid, "error": str(error)})
            continue

        report = _call_pull(
            PullView(transport),
            store,
            since=since,  # type: ignore[arg-type]
            authors=args.author,
            keyid=keyid or None,
            allow_legacy=allow_legacy,
        )
        totals["sessions_new"] += report.sessions_new
        totals["sessions_updated"] += report.sessions_updated
        totals["chunks"] += report.chunks
        totals["bytes"] += report.bytes
        # Per-key error prefix so a revoked key is named, not anonymous.
        all_errors += [f"{tag}: {problem}" for problem in report.errors]
        per_key.append(
            {
                "key": label,
                "keyid": keyid,
                "store": transport.describe(),
                "sessions_new": report.sessions_new,
                "sessions_updated": report.sessions_updated,
                "chunks": report.chunks,
                "bytes": report.bytes,
                "errors": report.errors,
            }
        )

    payload = {"keys": per_key, **totals, "errors": all_errors}
    lines = [f"keyring  {len(entries)} reader key(s)"]
    for row in per_key:
        if "error" in row and len(row) <= 3:
            lines.append(f"  {row['key']:<16}ERROR {row['error']}")
            continue
        lines.append(
            f"  {row['key']:<16}new {row.get('sessions_new', 0)}  "
            f"updated {row.get('sessions_updated', 0)}  "
            f"{_human(row.get('bytes', 0))}"
        )
    lines.append(f"pulled   {store.root / 'pulled'}")
    lines += [f"ERROR    {problem}" for problem in all_errors]
    if not all_errors and (totals["sessions_new"] or totals["sessions_updated"]):
        lines.append("run `ezcl collect --include-pulled -i` to journal them")
    _emit(payload, args.json, lines)
    return 0 if not all_errors else 1


def cmd_pull(args: argparse.Namespace) -> int:
    store = _store_for(args)
    allow_legacy = bool(getattr(args, "allow_legacy", False))

    # A populated keyring means this machine pulls as one or more reader keys,
    # each against its own store; an empty (or absent) keyring falls back to the
    # single device token this machine is configured with -- a dev's own pull,
    # unchanged.
    entries = _keyring_entries(_load_keyring(store))
    if entries:
        code = _pull_keyring(args, store, entries, allow_legacy)
        if code == 0:
            code = _journal_after_pull(args, store)
        return code

    config = load_config(store, os.getcwd())
    try:
        transport = transport_for(config)
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    since = _resolve_since(args)
    if since is False:
        return 2

    report = _call_pull(
        PullView(transport),
        store,
        since=since,  # type: ignore[arg-type]
        authors=args.author,
        keyid=None,
        allow_legacy=allow_legacy,
    )
    payload = {
        "store": transport.describe(),
        "sessions_new": report.sessions_new,
        "sessions_updated": report.sessions_updated,
        "chunks": report.chunks,
        "bytes": report.bytes,
        "errors": report.errors,
    }
    lines = [
        f"from     {transport.describe()}",
        f"new      {report.sessions_new} sessions",
        f"updated  {report.sessions_updated} sessions",
        f"fetched  {report.chunks} chunks, {_human(report.bytes)}",
        f"pulled   {store.root / 'pulled'}",
    ]
    lines += [f"ERROR    {problem}" for problem in report.errors]
    _emit(payload, args.json, lines)
    if not report.ok:
        return 1
    return _journal_after_pull(args, store)


def _journal_after_pull(args: argparse.Namespace, store: Store) -> int:
    """After a successful fetch, journal the pulled sessions -- unless asked not
    to. This is what makes `ezup pull` the PM's single command: fetch + report.
    The window defaults to 7 days; `--no-journal` (the runner) skips it.
    """
    if getattr(args, "no_journal", False):
        return 0
    window = str(getattr(args, "window", None) or "7d").strip()
    if window.isdigit():
        window = f"{window}d"        # bare "14" means 14 days
    elif window.lower() == "all":
        window = "2020-01-01"        # everything ezup could plausibly hold
    # Reuse the collect+pipeline path with a constructed namespace: pull over the
    # pulled sessions for the window, take them all, no picker.
    collect_args = argparse.Namespace(
        directories=[],
        since=window,
        until=None,
        no_recursive=False,
        match="any",
        min_turns=1,
        min_tools=1,
        interactive=False,
        yes=True,
        dry_run=False,
        stop_before_model=False,
        refresh=False,
        quiet=getattr(args, "quiet", False),
        limit=30,
        json=False,
        no_journal=False,
        include_pulled=True,
        store=getattr(args, "store", None),
    )
    return cmd_collect(collect_args)


def cmd_keyring(args: argparse.Namespace) -> int:
    """Manage the reader (``ezr_``) keys this machine pulls with.

    All storage/probe logic lives in :mod:`ezchangelog.keyring`; this command
    is the argparse surface over it. Loaded lazily so a build without that
    module still runs every other command.
    """
    store = _store_for(args)
    module = _keyring_module()
    if module is None:
        print(
            "error: reader keyring support is not available in this build "
            "(ezchangelog/keyring.py is missing)",
            file=sys.stderr,
        )
        return 2

    loader = getattr(module, "load_keyring", None)
    if not callable(loader):
        print("error: ezchangelog.keyring has no load_keyring()", file=sys.stderr)
        return 2
    try:
        keyring = loader(store)
    except Exception as error:  # a corrupt keyring must not crash the CLI
        print(f"error: could not load the keyring: {error}", file=sys.stderr)
        return 2

    try:
        if args.keyring_command == "add":
            # Refuse anything but a reader key before the module probes it, so
            # the error names the real problem rather than a downstream 401.
            try:
                key_set = parse_key(args.token)
            except CryptoError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            if key_set.kind != "reader":
                print(
                    "error: the keyring holds reader keys only; this is a "
                    f"{key_set.kind} key. A developer's own device key is "
                    "configured as the store token, not added here.",
                    file=sys.stderr,
                )
                return 2
            config = load_config(store, os.getcwd())
            # Keyring.add takes store=, not store_url= (the two build agents
            # diverged on the kwarg — review finding 1); and it does not
            # persist, so the caller saves.
            entry = keyring.add(
                args.token, label=args.label, store=config.store_url
            )
            keyring.save()
            keyid = _entry_field(entry, "keyid") or key_set.keyid
            label = _entry_field(entry, "label") or (args.label or "")
            _emit(
                {"added": {"keyid": keyid, "label": label}},
                args.json,
                [
                    f"added reader key {keyid[:8]}"
                    + (f" ({label})" if label else ""),
                    "the token is stored keyring-private and never printed back.",
                ],
            )
            return 0

        if args.keyring_command == "list":
            entries = _keyring_entries(keyring)
            rows = [
                {
                    "keyid": _entry_field(e, "keyid"),
                    "label": _entry_field(e, "label"),
                    "store": _entry_field(e, "store"),
                    "reader_id": _entry_field(e, "reader_id"),
                    "added_at": _entry_field(e, "added_at"),
                }
                for e in entries
            ]
            lines = [f"{len(rows)} reader key(s) in the keyring"]
            for row in rows:
                lines.append(
                    f"  {row['keyid'][:8]:<10}{_shorten(row['label'] or '-', 16)}"
                    f"{_shorten(row['store'] or '-', 34)}"
                    f"{_fmt_ts(row['added_at'])}"
                )
            # No code path prints a token (same discipline as Config.describe).
            _emit({"keys": rows}, args.json, lines)
            return 0

        # remove -- by label or keyid.
        removed = keyring.remove(args.selector)
        if removed:
            keyring.save()
        ok = bool(removed)
        _emit(
            {"removed": ok, "selector": args.selector},
            args.json,
            [
                f"removed {args.selector} from the keyring"
                if ok
                else f"no keyring entry matched {args.selector!r}",
                "pulled/ transcripts already fetched stay on disk; to stop a "
                "developer sharing, they must run `ezup token revoke`.",
            ],
        )
        return 0 if ok else 1
    except CryptoError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except TransportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except AttributeError as error:
        # The keyring module is present but missing a method this command
        # expects: report precisely instead of a raw traceback.
        print(f"error: keyring operation unsupported: {error}", file=sys.stderr)
        return 2


# -- statusline ---------------------------------------------------------------


def cmd_statusline(args: argparse.Namespace) -> int:
    """Print the one-line indicator Claude Code shows while a session runs.

    Delegates to the hook entry point so the `ezcl statusline` and
    `python -m ezchangelog.hook_entry statusline` forms cannot drift apart.
    """
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        payload = {}
    line = hook_entry.statusline(payload if isinstance(payload, dict) else {})
    if line:
        print(line)
    return 0


def cmd_hook_run(args: argparse.Namespace) -> int:
    """The stable entry the Claude Code plugin calls on every hook fire.

    Exists so the plugin can say `ezcl hook-run` and inherit hook_entry's
    never-raise guarantee, rather than needing to know which python owns the
    ezchangelog package.
    """
    return hook_entry.main([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ezcl",
        description=(
            "Scrape Claude Code sessions from project directories into a raw "
            "store for later changelog synthesis."
        ),
    )
    parser.add_argument(
        "--store",
        help="store root (default: $EZCHANGELOG_HOME or ~/.ezchangelog)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect",
        help="select sessions by directory + time window and copy them into the store",
    )
    collect_parser.add_argument(
        "directories",
        nargs="*",
        default=[],
        help="project directories to scrape; omit for every Claude session in the window",
    )
    collect_parser.add_argument(
        "--since",
        default="7d",
        help="relative duration (7d, 24h, 2w) or ISO-8601 instant; default 7d",
    )
    collect_parser.add_argument(
        "--until", help="end of the window; default now"
    )
    collect_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="match only the exact directories, not sessions from subdirectories",
    )
    collect_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="list the window's sessions and pick which ones to keep",
    )
    collect_parser.add_argument(
        "-y",
        "--yes",
        "--all",
        dest="yes",
        action="store_true",
        help=(
            "headless: take every matched session in the window without a "
            "picker and journal them straight through. Needs no TTY. Refuses "
            "with a non-zero exit when nothing matches, so an unattended runner "
            "surfaces a broken pull instead of emitting an empty journal."
        ),
    )
    collect_parser.add_argument(
        "--match",
        choices=("any", "cwd", "touched"),
        default="any",
        help=(
            "evidence used to tie a session to a directory: cwd (session started "
            "there), touched (session read/edited files there), or any. Default any, "
            "which catches sessions started outside the target folder."
        ),
    )
    collect_parser.add_argument(
        "--min-turns",
        type=int,
        default=1,
        help="drop sessions with fewer real user prompts (default 1)",
    )
    collect_parser.add_argument(
        "--min-tools",
        type=int,
        default=1,
        help="drop sessions with fewer tool calls (default 1: a session that "
        "ran nothing changed nothing). Use 0 to keep talk-only sessions.",
    )
    collect_parser.add_argument(
        "--dry-run", action="store_true", help="report selection without writing"
    )
    collect_parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-parse every transcript, ignoring cached index metadata",
    )
    collect_parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="max rows to print (0 = all); does not affect what is stored",
    )
    collect_parser.add_argument(
        "--include-pulled",
        action="store_true",
        help="also consider teammates' sessions fetched by `ezcl pull`",
    )
    collect_parser.add_argument("--json", action="store_true", help="emit the manifest as JSON")
    collect_parser.add_argument(
        "--no-journal",
        action="store_true",
        help="collect only; do not run the journal pipeline",
    )
    collect_parser.add_argument(
        "--stop-before-model",
        action="store_true",
        help="run distill and segment, then stop before any model call",
    )
    collect_parser.add_argument(
        "--quiet", action="store_true", help="pipeline prints stages but not model output"
    )
    collect_parser.set_defaults(func=cmd_collect)

    status_parser = subparsers.add_parser("status", help="summarize the store and index")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    hook_parser = subparsers.add_parser(
        "hook",
        help="wire ezup into ~/.claude/settings.json (installing shares nothing)",
    )
    hook_parser.add_argument(
        "action",
        choices=("install", "uninstall", "status"),
        help="install adds the hook + status line; uninstall removes only ours",
    )
    hook_parser.add_argument(
        "--settings", help="settings.json to edit (default ~/.claude/settings.json)"
    )
    hook_parser.add_argument("--json", action="store_true")
    hook_parser.set_defaults(func=cmd_hook)

    share_parser = subparsers.add_parser(
        "share",
        help="turn transcript sharing on or off for one session, and say why",
    )
    share_parser.add_argument(
        "action",
        choices=("on", "off", "status", "ack", "clear"),
        help=(
            "on/off set this session explicitly; status explains the current "
            "decision; ack accepts a repo's committed \"always\" policy on this "
            "machine; clear drops the session setting so the repo policy applies"
        ),
    )
    share_parser.add_argument(
        "--session",
        help=f"session id; default the one this command runs in (${share.SESSION_ENV})",
    )
    share_parser.add_argument("--json", action="store_true")
    share_parser.set_defaults(func=cmd_share)

    publish_parser = subparsers.add_parser(
        "publish",
        help="upload whatever of a shared session's transcript is not up there yet",
    )
    publish_parser.add_argument(
        "--session",
        help=f"session id; default the one this command runs in (${share.SESSION_ENV})",
    )
    publish_parser.add_argument(
        "--transcript",
        help="transcript file to publish; default the session's own file",
    )
    publish_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every byte range that would be sent, and send nothing",
    )
    publish_parser.add_argument("--json", action="store_true")
    publish_parser.set_defaults(func=cmd_publish)

    unpublish_parser = subparsers.add_parser(
        "unpublish", help="delete a session from the store and stop sharing it"
    )
    unpublish_parser.add_argument("--session", required=True, help="session id to delete")
    unpublish_parser.add_argument(
        "-y", "--yes", action="store_true", help="do not ask for confirmation"
    )
    unpublish_parser.set_defaults(func=cmd_unpublish)

    sync_parser = subparsers.add_parser(
        "sync",
        help="pick recent sessions and share their transcripts (nothing pre-ticked)",
    )
    sync_parser.add_argument(
        "window",
        nargs="?",
        default="7d",
        help="how far back to look: 7d, 24h, 2w, a date, or 'all'; default 7d",
    )
    sync_parser.add_argument(
        "directories",
        nargs="*",
        default=[],
        help="restrict to sessions belonging to these directories",
    )
    sync_parser.add_argument(
        "--min-turns", type=int, default=1, help="drop sessions with fewer user prompts"
    )
    sync_parser.add_argument(
        "--min-tools", type=int, default=1, help="drop sessions with fewer tool calls"
    )
    sync_parser.add_argument(
        "--enable",
        action="store_true",
        help="also turn sharing on for the picked sessions, so the hook keeps them current",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what each pick would send, and send nothing",
    )
    sync_parser.set_defaults(func=cmd_sync)

    pull_parser = subparsers.add_parser(
        "pull", help="fetch teammates' shared transcripts into <store>/pulled/"
    )
    pull_parser.add_argument(
        "--since",
        help="re-list everything changed since this point (7d, 2w, or a date); "
        "default resumes from the stored cursor",
    )
    pull_parser.add_argument(
        "--author",
        action="append",
        help="only this author; repeat for several",
    )
    pull_parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="also pull legacy plaintext sessions (no encryption, no wrapped "
        "key); off by default because such bytes are unverified -- the store "
        "operator could have substituted them",
    )
    pull_parser.add_argument(
        "window",
        nargs="?",
        default="7d",
        help="how far back to journal after fetching: 7d, 30d, a date, or "
        "'all'; default 7d. Bare number = days (e.g. 14).",
    )
    pull_parser.add_argument(
        "--no-journal",
        action="store_true",
        help="fetch only; do not build a journal (what the runner uses)",
    )
    pull_parser.add_argument("--json", action="store_true")
    pull_parser.set_defaults(func=cmd_pull)

    statusline_parser = subparsers.add_parser(
        "statusline",
        help="read hook JSON on stdin and print the one-line sharing indicator",
    )
    statusline_parser.set_defaults(func=cmd_statusline)

    hook_run_parser = subparsers.add_parser(
        "hook-run",
        help="process one hook event from stdin (used by the Claude Code plugin)",
    )
    hook_run_parser.set_defaults(func=cmd_hook_run)

    login_parser = subparsers.add_parser(
        "login",
        help="configure this machine with a device token you were handed",
    )
    login_parser.add_argument("token", help="the ezu_... device token")
    login_parser.add_argument("device_id", nargs="?", default="",
                              help="the device uuid printed beside the token")
    login_parser.add_argument("--store", help=f"store URL (default {DEFAULT_STORE_URL})")
    login_parser.add_argument("--author", help="display name (default: git email)")
    login_parser.add_argument("--no-device-id", action="store_true",
                              help="skip the device_id (read/publish only, no minting)")
    login_parser.add_argument("--force", action="store_true",
                              help="replace an existing device (orphans its sessions)")
    login_parser.add_argument("--json", action="store_true")
    login_parser.set_defaults(func=cmd_login)

    device_parser = subparsers.add_parser(
        "device",
        help="enrol a device so this machine can share and mint keys (admin-gated)",
    )
    device_sub = device_parser.add_subparsers(dest="token_command", required=True)
    for verb, helptext in (
        ("enroll", "enrol THIS machine as a device and write its config"),
        ("mint", "mint a device for someone else and print its key + id"),
    ):
        dp = device_sub.add_parser(verb, help=helptext)
        dp.add_argument("--name", required=True, help="who this device is for")
        dp.add_argument("--email", help="optional; defaults to <name>@ezup.local")
        dp.add_argument("--admin-token", help="admin token (else $EZUP_ADMIN_TOKEN "
                        "or ~/.ezchangelog/admin-token)")
        if verb == "enroll":
            dp.add_argument("--force", action="store_true",
                            help="replace an existing device config (orphans its sessions)")
        dp.add_argument("--json", action="store_true")
        dp.set_defaults(func=cmd_device, token_command=verb, force=False)

    token_parser = subparsers.add_parser(
        "token",
        help="mint, list or revoke read-only tokens for operators",
    )
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    mint = token_sub.add_parser(
        "mint", help="mint a read-only token scoped to your sessions"
    )
    mint.add_argument("--name", required=True, help="who this token is for")
    mint.add_argument("--json", action="store_true")
    mint.set_defaults(func=cmd_token, token_command="mint")
    show = token_sub.add_parser(
        "show", help="show this machine's device token (your web viewer login)"
    )
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_token, token_command="show")
    listing = token_sub.add_parser("list", help="list tokens you have minted")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_token, token_command="list")
    revoke = token_sub.add_parser("revoke", help="revoke a token you minted")
    revoke.add_argument(
        "id",
        nargs="?",
        default="",
        help="token id, id prefix, or name; omit when only one token is active",
    )
    revoke.add_argument("--json", action="store_true")
    revoke.set_defaults(func=cmd_token, token_command="revoke")

    keyring_parser = subparsers.add_parser(
        "keyring",
        help="manage the reader (ezr_) keys this machine pulls with",
    )
    keyring_sub = keyring_parser.add_subparsers(dest="keyring_command", required=True)
    kr_add = keyring_sub.add_parser(
        "add", help="add a reader key (ezr_...) a developer shared with you"
    )
    kr_add.add_argument("token", help="the pasted ezr_ reader key")
    kr_add.add_argument(
        "--label", help="a name for this key (default: the sessions' author)"
    )
    kr_add.add_argument("--json", action="store_true")
    kr_add.set_defaults(func=cmd_keyring, keyring_command="add")
    kr_list = keyring_sub.add_parser(
        "list", help="list the reader keys in the keyring (never prints tokens)"
    )
    kr_list.add_argument("--json", action="store_true")
    kr_list.set_defaults(func=cmd_keyring, keyring_command="list")
    kr_remove = keyring_sub.add_parser(
        "remove", help="drop a reader key from the keyring, by label or keyid"
    )
    kr_remove.add_argument("selector", help="the key's label or keyid")
    kr_remove.add_argument("--json", action="store_true")
    kr_remove.set_defaults(func=cmd_keyring, keyring_command="remove")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Die quietly when piped into `head` instead of dumping a BrokenPipeError.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
