"""Reading Claude Code transcripts and extracting mechanical facts.

A transcript is a JSONL file under ``~/.claude/projects/<slug>/<sessionId>.jsonl``.
The slug is a lossy encoding of the project path (``/`` and ``.`` both become
``-``), so it can never be decoded back into a directory. The authoritative
project path is the ``cwd`` field carried on every content-bearing entry, and
that is what directory matching uses.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator

from .window import isoformat, parse_timestamp

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Entry types that carry conversational or tool content.
CONTENT_TYPES = frozenset({"user", "assistant", "attachment", "system"})

# Tools whose use means a file was actually changed, not merely inspected.
MUTATING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

# Stamped on every prompt this tool sends to `claude -p`. Those calls write
# their own transcripts into ~/.claude/projects, and without a way to spot them
# the next run journals the journaller.
PIPELINE_MARKER = "[ezchangelog-pipeline]"


@dataclass
class SessionFacts:
    """Everything we can learn about one transcript without interpreting it."""

    session_id: str
    source_path: str
    project_slug: str
    cwds: list[str] = field(default_factory=list)
    git_branches: list[str] = field(default_factory=list)
    title: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    # Local dates on which this session had any activity. A session can span
    # months while only being touched on a handful of days, so first/last is
    # not a usable proxy for "was this worked on during the window".
    active_days: list[str] = field(default_factory=list)
    entry_count: int = 0
    entry_types: dict[str, int] = field(default_factory=dict)
    user_turns: int = 0
    assistant_turns: int = 0
    sidechain_entries: int = 0
    tool_uses: dict[str, int] = field(default_factory=dict)
    touched_paths: list[str] = field(default_factory=list)
    edited_paths: list[str] = field(default_factory=list)
    tool_generated: bool = False
    malformed_lines: int = 0

    @property
    def cwd(self) -> str | None:
        return self.cwds[0] if self.cwds else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cwd"] = self.cwd
        return data


def iter_entries(path: Path) -> Iterator[tuple[dict[str, Any] | None, str]]:
    """Yield ``(entry, raw_line)`` for each line; entry is None if unparseable."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield json.loads(line), line
            except json.JSONDecodeError:
                yield None, line


# Text that marks a user entry as harness bookkeeping rather than a real turn:
# slash-command echoes, their stdout, and the caveat block that precedes them.
_NOISE_MARKERS = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
)


def _user_text(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_real_turn(entry: dict[str, Any]) -> bool:
    """True when a user entry is something the user actually said."""
    if entry.get("isMeta"):
        return False
    text = _user_text(entry).strip()
    if not text:
        return False
    stripped = text
    for marker in _NOISE_MARKERS:
        stripped = stripped.replace(marker, "")
    return bool(stripped.strip()) and not text.startswith(_NOISE_MARKERS)


def _message_content(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _tool_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Pull filesystem paths out of a tool call, so docs-only vs code-touching
    sessions stay distinguishable downstream."""
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path", "scriptPath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    if tool_name in {"Read", "Edit", "Write", "NotebookEdit"} and not paths:
        return paths
    return paths


def scan_transcript(path: Path) -> SessionFacts:
    """Read a transcript once and derive its mechanical facts."""
    facts = SessionFacts(
        session_id=path.stem,
        source_path=str(path),
        project_slug=path.parent.name,
    )
    entry_types: Counter[str] = Counter()
    tool_uses: Counter[str] = Counter()
    cwds: list[str] = []
    branches: list[str] = []
    touched: list[str] = []
    edited: list[str] = []
    timestamps: list[str] = []

    for entry, _raw in iter_entries(path):
        if entry is None:
            facts.malformed_lines += 1
            continue
        entry_type = str(entry.get("type", "unknown"))
        entry_types[entry_type] += 1
        facts.entry_count += 1

        if entry_type == "ai-title" and isinstance(entry.get("aiTitle"), str):
            facts.title = entry["aiTitle"]

        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd and cwd not in cwds:
            cwds.append(cwd)
        branch = entry.get("gitBranch")
        if isinstance(branch, str) and branch and branch not in branches:
            branches.append(branch)

        if parse_timestamp(entry.get("timestamp")) is not None:
            timestamps.append(entry["timestamp"])

        if entry.get("isSidechain"):
            facts.sidechain_entries += 1

        if entry_type == "user" and is_real_turn(entry):
            facts.user_turns += 1
            if PIPELINE_MARKER in _user_text(entry):
                facts.tool_generated = True
        elif entry_type == "assistant":
            facts.assistant_turns += 1
            for block in _message_content(entry):
                if block.get("type") != "tool_use":
                    continue
                tool_name = str(block.get("name", "unknown"))
                tool_uses[tool_name] += 1
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    for candidate in _tool_paths(tool_name, tool_input):
                        if candidate not in touched:
                            touched.append(candidate)
                        if tool_name in MUTATING_TOOLS and candidate not in edited:
                            edited.append(candidate)

    parsed = sorted(
        (moment for moment in (parse_timestamp(t) for t in timestamps) if moment)
    )
    if parsed:
        facts.first_timestamp = isoformat(parsed[0])
        facts.last_timestamp = isoformat(parsed[-1])
        facts.active_days = sorted(
            {moment.astimezone().strftime("%Y-%m-%d") for moment in parsed}
        )

    facts.cwds = cwds
    facts.git_branches = branches
    facts.entry_types = dict(entry_types)
    facts.tool_uses = dict(tool_uses)
    facts.touched_paths = touched
    facts.edited_paths = edited
    return facts


def discover_transcripts(projects_dir: Path | None = None) -> list[Path]:
    """List every transcript file across all project directories."""
    root = projects_dir or CLAUDE_PROJECTS_DIR
    if not root.is_dir():
        return []
    found: list[Path] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        found.extend(sorted(project_dir.glob("*.jsonl")))
    return found


def stat_signature(path: Path) -> dict[str, Any]:
    """Cheap change-detection key: a re-scan is skipped when this matches."""
    info = os.stat(path)
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
