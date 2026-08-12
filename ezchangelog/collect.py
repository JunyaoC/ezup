"""Mechanical collection: directories + time window -> raw sessions in the store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .store import Store
from .transcripts import (
    SessionFacts,
    discover_transcripts,
    scan_transcript,
    stat_signature,
)
from .window import isoformat, parse_timestamp


@dataclass
class Selection:
    """One transcript selected by the directory + window filters."""

    facts: SessionFacts
    matched_root: str
    match_reason: str
    reused_from_index: bool
    stored_path: str | None = None
    copied: bool = False
    window_days: list[str] = field(default_factory=list)
    # Empty for sessions this machine recorded; the teammate's name for ones
    # pulled from the team store. Everything downstream treats the two alike,
    # so the author is the only thing that distinguishes them.
    author: str = ""
    pulled: bool = False
    # Filled in by `ezcl sync` from the publish state, purely for display.
    synced: str = ""

    @property
    def last_active(self) -> str:
        """Last day of work inside the window, not the session's lifetime."""
        return self.window_days[-1] if self.window_days else (
            (self.facts.last_timestamp or "")[:10]
        )

    @property
    def display_dir(self) -> str:
        """The session's directory, shown relative to the matched root."""
        cwd = self.facts.cwd
        if not cwd:
            return self.facts.project_slug
        if not self.matched_root:
            return cwd.replace(str(Path.home()), "~", 1)
        root = Path(self.matched_root)
        # A session can change directory partway through. Show the cwd that
        # actually put it in scope, not whichever came first.
        for candidate in self.facts.cwds:
            if _contains(root, candidate, True):
                cwd = candidate
                break
        try:
            relative = Path(cwd).relative_to(root)
        except ValueError:
            return cwd.replace(str(Path.home()), "~", 1)
        return f"{root.name}/{relative}" if relative.parts else root.name


@dataclass
class CollectResult:
    run_id: str
    roots: list[str]
    since: datetime
    until: datetime
    recursive: bool
    selected: list[Selection] = field(default_factory=list)
    scanned_files: int = 0
    reused_files: int = 0
    parsed_files: int = 0
    skipped_out_of_window: int = 0
    skipped_out_of_scope: int = 0
    skipped_empty: int = 0
    skipped_internal: int = 0
    deselected: int = 0

    def manifest(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": isoformat(datetime.now(timezone.utc)),
            "roots": self.roots,
            "window": {"since": isoformat(self.since), "until": isoformat(self.until)},
            "recursive": self.recursive,
            "counts": {
                "transcripts_seen": self.scanned_files,
                "metadata_reused": self.reused_files,
                "transcripts_parsed": self.parsed_files,
                "selected": len(self.selected),
                "skipped_out_of_scope": self.skipped_out_of_scope,
                "skipped_out_of_window": self.skipped_out_of_window,
                "skipped_empty": self.skipped_empty,
                "skipped_internal": self.skipped_internal,
                "deselected_by_user": self.deselected,
            },
            "sessions": [
                {
                    **selection.facts.to_dict(),
                    "matched_root": selection.matched_root,
                    "match_reason": selection.match_reason,
                    "stored_path": selection.stored_path,
                    "copied_this_run": selection.copied,
                    "author": selection.author,
                    "pulled": selection.pulled,
                }
                for selection in self.selected
            ],
        }


def normalize_roots(paths: list[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in paths:
        resolved = Path(raw).expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _contains(root: Path, raw: str, recursive: bool) -> bool:
    try:
        candidate = Path(raw).expanduser().resolve()
    except (OSError, ValueError):
        return False
    if candidate == root:
        return True
    return recursive and root in candidate.parents


def match_root(
    facts: SessionFacts, roots: list[Path], recursive: bool, mode: str = "any"
) -> tuple[str, str] | None:
    """Return ``(root, reason)`` for the first root this session belongs to.

    Matching uses real path containment -- project-directory slugs are lossy
    and cannot be trusted here. Two independent kinds of evidence:

    ``cwd``      the session was started inside the directory
    ``edited``   the session wrote files inside the directory
    ``read``     the session only read files inside the directory

    A session started in a parent directory but spent editing files in the
    target still counts, which is why ``any`` is the default.

    With no roots at all, every session matches: the window is the only filter.
    """
    if not roots:
        return "", "all"
    for root in roots:
        if mode in ("any", "cwd"):
            if any(_contains(root, cwd, recursive) for cwd in facts.cwds):
                return str(root), "cwd"
        if mode in ("any", "touched"):
            if any(_contains(root, p, recursive) for p in facts.edited_paths):
                return str(root), "edited"
            if any(_contains(root, p, recursive) for p in facts.touched_paths):
                return str(root), "read"
    return None


def active_days_in(
    facts: SessionFacts, since: datetime, until: datetime
) -> list[str]:
    """The local dates this session was actually worked on inside the window."""
    low = since.astimezone().strftime("%Y-%m-%d")
    # `until` is exclusive: a bare end date was pushed to the next local
    # midnight, so step back inside the window before naming its last day.
    high = (until - timedelta(microseconds=1)).astimezone().strftime("%Y-%m-%d")
    return [day for day in facts.active_days if low <= day <= high]


def overlaps_window(
    facts: SessionFacts, since: datetime, until: datetime
) -> bool:
    """True when the session was worked on during the window.

    Not mere overlap: a session opened in June and last touched in August
    straddles every window in between without having been touched in any of
    them. Selection follows the days that actually carry entries.
    """
    if facts.active_days:
        return bool(active_days_in(facts, since, until))
    first = parse_timestamp(facts.first_timestamp)
    last = parse_timestamp(facts.last_timestamp)
    if first is None or last is None:
        return False
    return first <= until and last >= since


def _facts_for(
    path: Path,
    sources: dict[str, Any],
    signature: dict[str, Any],
    refresh: bool,
) -> tuple[SessionFacts, bool]:
    """Facts for one transcript, reusing the index when the file is unchanged.

    Returns ``(facts, reused)``. The index entry is created or refreshed as a
    side effect, exactly as the scan loop needs it.
    """
    key = str(path)
    cached = sources.get(key)
    unchanged = (
        not refresh
        and isinstance(cached, dict)
        and cached.get("signature") == signature
        and isinstance(cached.get("facts"), dict)
    )
    if unchanged:
        return (
            SessionFacts(**{
                field_name: cached["facts"][field_name]
                for field_name in SessionFacts.__dataclass_fields__
                if field_name in cached["facts"]
            }),
            True,
        )

    facts = scan_transcript(path)
    sources[key] = {
        "signature": signature,
        "facts": facts.to_dict(),
        "stored_path": (cached or {}).get("stored_path"),
        "ingested_at": (cached or {}).get("ingested_at"),
        "ingested_signature": (cached or {}).get("ingested_signature"),
    }
    return facts, False


def _collect_pulled(
    result: CollectResult,
    store: Store,
    sources: dict[str, Any],
    roots: list[Path],
    since: datetime,
    until: datetime,
    *,
    recursive: bool,
    match_mode: str,
    min_turns: int,
    min_tools: int,
    refresh: bool,
) -> None:
    """Add teammates' transcripts from ``<store>/pulled/`` to the selection.

    A pulled transcript is a byte-identical copy of what the teammate had on
    disk, so it goes through the same filters as a local one. Two things differ:
    it carries an author, and it is already inside the store, so the ingest step
    must not copy it into ``raw/`` a second time.
    """
    from .pull import pulled_sessions  # imported here: pull is PM-side only

    for row in pulled_sessions(store):
        if not row.get("present"):
            continue  # listed in the pull state but not (yet) on disk
        path = Path(str(row.get("path")))
        result.scanned_files += 1
        try:
            signature = stat_signature(path)
        except OSError:
            continue

        facts, reused = _facts_for(path, sources, signature, refresh)
        if reused:
            result.reused_files += 1
        else:
            result.parsed_files += 1

        if facts.tool_generated:
            result.skipped_internal += 1
            continue
        matched = match_root(facts, roots, recursive, match_mode)
        if matched is None:
            result.skipped_out_of_scope += 1
            continue
        if not overlaps_window(facts, since, until):
            result.skipped_out_of_window += 1
            continue
        if facts.user_turns < min_turns or sum(facts.tool_uses.values()) < min_tools:
            result.skipped_empty += 1
            continue

        root, reason = matched
        result.selected.append(
            Selection(
                facts=facts,
                matched_root=root,
                match_reason=reason,
                reused_from_index=reused,
                stored_path=str(path),
                window_days=active_days_in(facts, since, until),
                author=str(row.get("author") or ""),
                pulled=True,
            )
        )


def collect(
    roots: list[Path],
    since: datetime,
    until: datetime,
    store: Store,
    *,
    recursive: bool = True,
    match_mode: str = "any",
    min_turns: int = 1,
    min_tools: int = 1,
    dry_run: bool = False,
    refresh: bool = False,
    include_pulled: bool = False,
    projects_dir: Path | None = None,
    run_id: str | None = None,
    chooser: Callable[[list[Selection]], list[Selection]] | None = None,
) -> CollectResult:
    result = CollectResult(
        run_id=run_id or f"run-{isoformat(datetime.now(timezone.utc))}".replace(":", ""),
        roots=[str(root) for root in roots],
        since=since,
        until=until,
        recursive=recursive,
    )

    index = store.load_index()
    sources: dict[str, Any] = index.setdefault("sources", {})

    for path in discover_transcripts(projects_dir):
        result.scanned_files += 1
        key = str(path)
        try:
            signature = stat_signature(path)
        except OSError:
            continue

        facts, unchanged = _facts_for(path, sources, signature, refresh)
        if unchanged:
            result.reused_files += 1
        else:
            result.parsed_files += 1

        if facts.tool_generated or any(
            _contains(store.root, cwd, True) for cwd in facts.cwds
        ):
            # The pipeline's own headless calls. Never journal yourself.
            result.skipped_internal += 1
            continue

        matched = match_root(facts, roots, recursive, match_mode)
        if matched is None:
            result.skipped_out_of_scope += 1
            continue
        if not overlaps_window(facts, since, until):
            result.skipped_out_of_window += 1
            continue
        if facts.user_turns < min_turns or sum(facts.tool_uses.values()) < min_tools:
            # Nothing was said, or nothing was done. Either way there is no
            # change to report: a session with zero tool calls touched nothing.
            result.skipped_empty += 1
            continue

        root, reason = matched
        record = sources.setdefault(key, {"signature": signature})
        result.selected.append(
            Selection(
                facts=facts,
                matched_root=root,
                match_reason=reason,
                reused_from_index=unchanged,
                stored_path=record.get("stored_path"),
                window_days=active_days_in(facts, since, until),
            )
        )

    if include_pulled:
        _collect_pulled(
            result,
            store,
            sources,
            roots,
            since,
            until,
            recursive=recursive,
            match_mode=match_mode,
            min_turns=min_turns,
            min_tools=min_tools,
            refresh=refresh,
        )

    # Newest first, then cluster by author so a mixed local+pulled list groups
    # each person's sessions together (a stable sort keeps the date order within
    # each author). Local sessions have no author and sort as one group.
    result.selected.sort(key=lambda s: s.last_active, reverse=True)
    result.selected.sort(key=lambda s: (s.author or "").lower())

    # Let the caller narrow the selection before anything is written.
    if chooser is not None:
        chosen = chooser(result.selected)
        result.deselected = len(result.selected) - len(chosen)
        result.selected = chosen

    if dry_run:
        return result

    store.ensure()
    for selection in result.selected:
        if selection.pulled:
            # Already inside the store, and not ours to re-file under raw/.
            continue
        record = sources.setdefault(selection.facts.source_path, {})
        signature = record.get("signature")
        already_ingested = (
            record.get("ingested_signature") == signature
            and record.get("stored_path")
            and Path(record["stored_path"]).is_file()
        )
        if already_ingested:
            selection.stored_path = record["stored_path"]
            continue
        target = store.copy_raw(
            Path(selection.facts.source_path),
            selection.facts.project_slug,
            selection.facts.session_id,
        )
        selection.stored_path = str(target)
        selection.copied = True
        record["stored_path"] = str(target)
        record["ingested_signature"] = signature
        record["ingested_at"] = isoformat(datetime.now(timezone.utc))

    store.save_index(index)
    store.write_run(result.run_id, result.manifest())
    return result
