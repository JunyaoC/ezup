"""``ezcl`` command line interface (phase 1: mechanical extraction only)."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import picker
from .collect import CollectResult, collect, normalize_roots
from .console import Console
from .pipeline import STAGES as pipeline_stages_const, run_pipeline


def pipeline_stages() -> list[str]:
    return list(pipeline_stages_const)
from .store import Store, default_store
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


def _session_row(index: int | None, selection) -> str:
    facts = selection.facts
    tools = sum(facts.tool_uses.values())
    prefix = f"{index:>3}. " if index is not None else ""
    days = f" +{len(selection.window_days) - 1}d" if len(selection.window_days) > 1 else ""
    return (
        f"{prefix}"
        f"{selection.last_active + days:<17}"
        f"{selection.match_reason:<7}"
        f"{facts.user_turns:>6}  "
        f"{tools:>6}  "
        f"{_shorten(selection.display_dir, 30)}  "
        f"{_shorten(facts.title or facts.session_id[:8], 44)}"
    )


def _table_header(numbered: bool) -> str:
    prefix = "  #  " if numbered else ""
    return (
        f"{prefix}{'ACTIVE':<17}{'WHY':<7}"
        f"{'TURNS':>6}  {'TOOLS':>6}  {'DIRECTORY':<30}  {'TITLE'}"
    )


def interactive_chooser(selections: list) -> list:
    """Let the user pick which sessions to keep.

    Uses the full-screen checkbox picker on a terminal, and a numbered prompt
    when stdin or stdout is redirected.
    """
    if not selections:
        return []
    if sys.stdin.isatty() and sys.stdout.isatty():
        chosen = picker.pick(selections)
        if chosen is picker.ABORTED:
            print("cancelled; nothing collected", file=sys.stderr)
            raise SystemExit(1)
        return chosen
    return numbered_chooser(selections)


def numbered_chooser(selections: list) -> list:
    print(_table_header(numbered=True))
    for position, selection in enumerate(selections, start=1):
        print(_session_row(position, selection))
    print()

    while True:
        try:
            answer = input(
                f"select 1-{len(selections)} (e.g. 1,3,5-8) "
                f"[a=all, n=none, Enter=all]: "
            )
        except (EOFError, KeyboardInterrupt):
            print()
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
        print(_table_header(numbered=False))
        shown = result.selected[:limit] if limit > 0 else result.selected
        for selection in shown:
            print(_session_row(None, selection))
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
    roots = normalize_roots(args.directories)
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
        chooser=interactive_chooser if args.interactive else None,
    )

    if args.json:
        json.dump(result.manifest(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    will_journal = not (args.no_journal or args.dry_run) and result.selected
    # The picker already showed the sessions; reprinting the whole table before
    # the pipeline just pushes the interesting part off screen.
    if not (will_journal and args.interactive):
        _print_result(result, store, args.dry_run, args.limit)

    if not will_journal:
        return 0

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
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(Path(args.store).expanduser() if args.store else default_store())
    index = store.load_index()
    sources = index.get("sources", {})
    ingested = [s for s in sources.values() if s.get("stored_path")]
    runs = sorted(store.runs_dir.glob("*.json")) if store.runs_dir.is_dir() else []

    payload = {
        "store": str(store.root),
        "exists": store.root.is_dir(),
        "index_updated_at": index.get("updated_at"),
        "transcripts_indexed": len(sources),
        "transcripts_ingested": len(ingested),
        "runs": len(runs),
        "latest_run": runs[-1].stem if runs else None,
    }
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for key, value in payload.items():
            print(f"{key:<22}{value}")
    return 0


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
