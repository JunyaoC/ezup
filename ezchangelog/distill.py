"""Deterministic distillation: transcript -> action tree -> goals and attempts.

No model runs here. Every transcript entry carries ``parentUuid``, so a session
is already a DAG: branch points are the moments the user rewound and tried
something else. This module recovers that structure, labels which paths
survived, reads outcomes from tool results, and prunes the result down to
something small enough to hand to a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Tools that change the world, versus tools that merely look at it.
MUTATING = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
EXPLORING = frozenset({"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch"})

VERBATIM_LIMIT = 600
COMMAND_LIMIT = 240
ERROR_LIMIT = 300
NOTE_LIMIT = 240


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Node:
    id: str
    parent: str | None
    kind: str  # directive | action | outcome | note | snapshot
    ts: str | None = None
    tool: str | None = None
    paths: list[str] = field(default_factory=list)
    verbatim: str = ""
    status: str | None = None  # ok | error, on outcome nodes
    excerpt: str = ""  # the actual text written, for mutating actions
    tool_use_id: str | None = None
    is_sidechain: bool = False
    lane: str = "trunk"
    order: int = 0


def _iter_json(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


EXCERPT_LIMIT = 420


def _tool_excerpt(tool: str, payload: dict[str, Any]) -> str:
    """The text a mutating call actually wrote -- real code, not a summary."""
    if tool not in MUTATING:
        return ""
    for key in ("new_string", "content", "new_source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            body = value.strip()
            return body if len(body) <= EXCERPT_LIMIT else body[:EXCERPT_LIMIT] + "\n…"
    return ""


def _tool_target(tool: str, payload: dict[str, Any]) -> tuple[list[str], str]:
    """Reduce a tool call to (paths, one-line verbatim)."""
    paths = [
        payload[key]
        for key in ("file_path", "notebook_path", "path")
        if isinstance(payload.get(key), str) and payload[key]
    ]
    if tool == "Bash":
        return paths, _clip(payload.get("command", ""), COMMAND_LIMIT)
    if tool in MUTATING:
        return paths, _clip(paths[0] if paths else "", COMMAND_LIMIT)
    if tool == "Task":
        return paths, _clip(payload.get("description", ""), COMMAND_LIMIT)
    return paths, _clip(json.dumps(payload)[:COMMAND_LIMIT], COMMAND_LIMIT)


def build_tree(transcript: Path) -> list[Node]:
    """Recover the action tree from one transcript."""
    nodes: dict[str, Node] = {}
    order: list[str] = []
    # One transcript entry can yield several nodes (three tool calls in one
    # assistant turn). Children reference the *entry* uuid, so remember which
    # node stands in for each entry and rewrite parents once the pass is done.
    stands_for: dict[str, str] = {}

    def remember(entry_uuid: str, node_id: str) -> None:
        stands_for.setdefault(entry_uuid, node_id)

    # Every entry's link, including the types that produce no node (system,
    # attachment, meta). Without these the ancestry walk hits a hole and stops.
    raw_parent: dict[str, str | None] = {}

    for index, entry in enumerate(_iter_json(transcript)):
        uuid = entry.get("uuid")
        entry_type = entry.get("type")
        if uuid:
            raw_parent[uuid] = entry.get("parentUuid")

        if entry_type == "file-history-snapshot":
            continue
        if not uuid or entry_type not in ("user", "assistant"):
            continue

        parent = entry.get("parentUuid")
        ts = entry.get("timestamp")
        sidechain = bool(entry.get("isSidechain"))
        blocks = _blocks(entry)

        # A user entry is either a directive, or the carrier of tool results.
        if entry_type == "user":
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if results:
                for block in results:
                    use_id = block.get("tool_use_id")
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    node = Node(
                        id=f"{uuid}:{use_id}",
                        parent=parent,
                        kind="outcome",
                        ts=ts,
                        status="error" if block.get("is_error") else "ok",
                        verbatim=_clip(body or "", ERROR_LIMIT)
                        if block.get("is_error")
                        else "",
                        tool_use_id=use_id,
                        is_sidechain=sidechain,
                        order=index,
                    )
                    nodes[node.id] = node
                    order.append(node.id)
                    remember(uuid, node.id)
                continue
            if entry.get("isMeta"):
                continue
            text = " ".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ).strip()
            if not text:
                continue
            node = Node(
                id=uuid,
                parent=parent,
                kind="directive",
                ts=ts,
                verbatim=_clip(text, VERBATIM_LIMIT),
                is_sidechain=sidechain,
                order=index,
            )
            nodes[uuid] = node
            order.append(uuid)
            remember(uuid, uuid)
            continue

        # Assistant: tool calls become actions, prose becomes a note.
        actions = [b for b in blocks if b.get("type") == "tool_use"]
        prose = " ".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        ).strip()

        if prose and not actions:
            node = Node(
                id=uuid,
                parent=parent,
                kind="note",
                ts=ts,
                verbatim=_clip(prose, NOTE_LIMIT),
                is_sidechain=sidechain,
                order=index,
            )
            nodes[uuid] = node
            order.append(uuid)
            remember(uuid, uuid)

        for position, block in enumerate(actions):
            tool = str(block.get("name", "?"))
            payload = block.get("input") if isinstance(block.get("input"), dict) else {}
            paths, verbatim = _tool_target(tool, payload)
            node = Node(
                id=uuid if position == 0 else f"{uuid}#{position}",
                parent=parent,
                kind="action",
                ts=ts,
                tool=tool,
                paths=paths,
                verbatim=verbatim,
                excerpt=_tool_excerpt(tool, payload),
                tool_use_id=block.get("id"),
                is_sidechain=sidechain,
                order=index,
            )
            nodes[node.id] = node
            order.append(node.id)
            remember(uuid, node.id)

    def resolve(parent_uuid: str | None) -> str | None:
        """Climb past entries that produced no node until one did."""
        hops = 0
        while parent_uuid and parent_uuid not in stands_for and hops < 1000:
            parent_uuid = raw_parent.get(parent_uuid)
            hops += 1
        return stands_for.get(parent_uuid) if parent_uuid else None

    for node in nodes.values():
        node.parent = resolve(node.parent)

    _label_lanes(nodes, order)
    return [nodes[i] for i in order]


def _label_lanes(nodes: dict[str, Node], order: list[str]) -> None:
    """Mark the surviving path as trunk; everything else is abandoned.

    The trunk is the ancestry of the chronologically last node. ``last-prompt``
    carries a ``leafUuid``, but that marks the last *prompt*, not the session's
    final node, so it under-reports the trunk.
    """
    if not order:
        return
    for node in nodes.values():
        node.lane = "abandoned"

    trunk: set[str] = set()
    cursor: str | None = order[-1]
    seen: set[str] = set()
    while cursor and cursor in nodes and cursor not in seen:
        seen.add(cursor)
        trunk.add(cursor)
        cursor = nodes[cursor].parent

    # Actions and outcomes hang off the same parent as their assistant turn, so
    # anything whose parent is on the trunk is on the trunk too.
    for node in nodes.values():
        if node.id in trunk or (node.parent and node.parent in trunk):
            node.lane = "trunk"


# --- segmentation ---------------------------------------------------------


@dataclass
class Attempt:
    actions: list[Node]
    lane: str
    errors: int

    def to_dict(self, verbose: bool = False, limit: int = 14) -> dict[str, Any]:
        """Compact form for a model.

        Exploration (Read/Grep/Glob) is counted, not listed: it says where
        attention went, never what changed. Consecutive repeats of one tool on
        one target collapse to a single line with a count.
        """
        kept: list[dict[str, Any]] = []
        skipped = 0
        for action in self.actions:
            if action.tool in EXPLORING:
                skipped += 1
                continue
            row = {"id": action.id, "tool": action.tool, "target": action.verbatim}
            if kept and kept[-1]["tool"] == row["tool"] and kept[-1]["target"] == row["target"]:
                kept[-1]["repeated"] = kept[-1].get("repeated", 1) + 1
                continue
            kept.append(row)
        overflow = max(0, len(kept) - limit) if not verbose else 0
        payload: dict[str, Any] = {
            "lane": self.lane,
            "errors": self.errors,
            "actions": kept if verbose else kept[:limit],
        }
        if skipped:
            payload["explored"] = skipped
        if overflow:
            payload["more_actions"] = overflow
        return payload


@dataclass
class Goal:
    id: str
    directive: str
    ts: str | None
    lane: str
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self, verbose: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "directive": self.directive,
            "ts": self.ts,
            "lane": self.lane,
            "attempts": [a.to_dict(verbose) for a in self.attempts],
            "files": sorted(
                {p for a in self.attempts for n in a.actions
                 for p in n.paths if n.tool in MUTATING}
            )[:20],
        }


def segment(nodes: list[Node]) -> list[Goal]:
    """Cut the tree into goals (opened by a directive) and their attempts."""
    outcome_by_use: dict[str, Node] = {
        n.tool_use_id: n for n in nodes if n.kind == "outcome" and n.tool_use_id
    }

    goals: list[Goal] = []
    current: Goal | None = None
    bucket: list[Node] = []

    def flush() -> None:
        nonlocal bucket
        if current is None or not bucket:
            bucket = []
            return
        for lane in ("trunk", "abandoned"):
            group = [n for n in bucket if n.lane == lane]
            if not group:
                continue
            errors = sum(
                1
                for n in group
                if n.tool_use_id
                and outcome_by_use.get(n.tool_use_id, Node("", None, "")).status
                == "error"
            )
            current.attempts.append(Attempt(actions=group, lane=lane, errors=errors))
        bucket = []

    for node in nodes:
        if node.kind == "directive" and not node.is_sidechain:
            flush()
            current = Goal(
                id=node.id, directive=node.verbatim, ts=node.ts, lane=node.lane
            )
            goals.append(current)
        elif node.kind == "action":
            bucket.append(node)
    flush()
    return goals


def prune(goals: list[Goal], max_goals: int = 120) -> list[Goal]:
    """Drop goals that carry no evidence of work.

    A rewound prompt with no action behind it is a typo, not a dead end. This
    is the materiality rule: an abandoned branch only counts as *tried* when
    something was actually run or written.
    """
    kept: list[Goal] = []
    for goal in goals:
        goal.attempts = [
            a
            for a in goal.attempts
            if a.lane == "trunk"
            or any(n.tool in MUTATING or n.tool == "Bash" for n in a.actions)
        ]
        if goal.attempts or goal.lane == "trunk":
            kept.append(goal)
    return kept[:max_goals]


def excerpts(nodes: list[Node], session_id: str, limit: int = 60) -> list[dict[str, Any]]:
    """Real code written during the session, one row per changed file.

    Kept out of the BRIEF digest (it would double its size) and handed only to
    the LINK pass, which needs actual code to attach to a delivered claim.
    """
    latest: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if node.kind != "action" or not node.excerpt or not node.paths:
            continue
        path = node.paths[0]
        latest[path] = {
            "id": f"{session_id[:8]}#{node.id[:8]}",
            "file": path,
            "code": node.excerpt,
            "ts": node.ts,
        }
    rows = sorted(latest.values(), key=lambda r: r["ts"] or "")
    return rows[-limit:]


def stats(nodes: list[Node], goals: list[Goal]) -> dict[str, Any]:
    actions = [n for n in nodes if n.kind == "action"]
    outcomes = [n for n in nodes if n.kind == "outcome"]
    edited = {p for n in actions if n.tool in MUTATING for p in n.paths}
    return {
        "nodes": len(nodes),
        "directives": sum(1 for n in nodes if n.kind == "directive"),
        "actions": len(actions),
        "errors": sum(1 for n in outcomes if n.status == "error"),
        "abandoned": sum(1 for n in nodes if n.lane == "abandoned"),
        "goals": len(goals),
        "files_edited": len(edited),
    }
