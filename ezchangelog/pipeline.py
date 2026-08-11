"""The journal pipeline: picked sessions in, one HTML file out.

    DISTILL   code           transcript -> action tree (trunk / abandoned)
    SEGMENT   code           tree -> goals -> attempts -> outcomes
    BRIEF     sonnet, low    one session's goals -> what was done/tried/failed
    COMPOSE   opus, max      all briefs -> the journal's entries
    RENDER    code           entries -> journal.md + journal.html

Only COMPOSE forms judgment, so only COMPOSE gets the expensive model. Every
model call is stateless (see ``llm.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import git, llm
from .console import Console
from .distill import build_tree, excerpts, prune, segment, stats
from .render import render_html, render_markdown
from .store import Store
from .transcripts import PIPELINE_MARKER
from .window import isoformat, parse_timestamp

STAGES = ["DISTILL", "SEGMENT", "GIT", "BRIEF", "COMPOSE", "LINK", "RENDER"]

BRIEF_SYSTEM = (
    "You convert a structured log of one working session into factual notes for "
    "a status report. You write for a manager, not an engineer: outcomes and "
    "decisions, not file paths or function names. You never invent work that is "
    "not in the input and never propose future work. You reply with JSON only."
)

COMPOSE_SYSTEM = (
    "You write a work journal for people who did not do the work: founders, "
    "managers, clients. Plain language, outcomes first, no jargon, no file "
    "paths in prose. Every line traces to the input. You never invent work, "
    "never speculate about intent, and never mention future or recommended "
    "work. You reply with JSON only."
)


def _brief_prompt(session: dict[str, Any], goals: list[dict[str, Any]]) -> str:
    return f"""{PIPELINE_MARKER}
Below is one Claude Code session, reduced to its action tree.

Session: {session.get('title') or session['session_id'][:8]}
Directory: {session.get('cwd')}
When: {session.get('first_timestamp')} to {session.get('last_timestamp')}

Each goal is a user instruction plus the attempts made to satisfy it.
`lane` is mechanical, not a judgement:
  - "trunk"     = this work survived to the end of the session
  - "abandoned" = the user rewound past it; it was tried and dropped
`errors` counts tool calls that actually failed.

Report ONLY what the data shows. If a section has nothing, use an empty list.

GOALS:
{json.dumps(goals, indent=1)[:180000]}

Write for a status report a manager reads. Describe outcomes in plain language.
Do not name files, functions or flags in prose — put technical detail in
`snippets` only when a reader genuinely needs it to understand the change.

A blocker is only worth reporting WITH how it was resolved. If the data does
not show a resolution, leave it out entirely rather than guessing.

Reply with this JSON shape and nothing else:
{{
  "worked_on": ["short phrase naming a thread of work"],
  "delivered": [{{"what": "what now exists, in plain language", "goal": "<goal id>"}}],
  "decisions": [{{"what": "the approach settled on and why it was chosen",
                  "goal": "<goal id>"}}],
  "blockers": [{{"what": "what got in the way",
                 "resolution": "how it was resolved or worked around — required",
                 "goal": "<goal id>"}}],
  "milestones": [{{"goal": "<goal id>", "what": "one short beat of progress"}}],
  "snippets": [{{"caption": "why this matters", "code": "the command or value",
                 "goal": "<goal id>"}}],
  "kind": "code|docs|content|infra|config"
}}"""


LINK_SYSTEM = (
    "You attach evidence to claims. You only ever pick from the candidates "
    "given to you, by id. You never write code, never invent a file name, and "
    "never attach a reference you are unsure of. You reply with JSON only."
)


def _link_prompt(
    entries: list[dict[str, Any]],
    excerpts: list[dict[str, Any]],
    commits: list[dict[str, Any]],
) -> str:
    claims = [
        {
            "entry": entry.get("id") or entry.get("title"),
            "title": entry.get("title"),
            "delivered": entry.get("delivered") or [],
        }
        for entry in entries
    ]
    slim_excerpts = [{"id": e["id"], "file": e["file"]} for e in excerpts]
    slim_commits = [
        {"sha": c["short"], "subject": c["subject"], "files": c["files"][:8]}
        for c in commits
    ]
    return f"""{PIPELINE_MARKER}
For each delivered claim below, attach the code that backs it.

Pick ONLY from the candidate ids given. If nothing clearly matches a claim,
attach nothing for it -- a wrong reference is worse than none.

CLAIMS:
{json.dumps(claims, indent=1)[:120000]}

CANDIDATE FILES (id -> path):
{json.dumps(slim_excerpts, indent=1)[:60000]}

CANDIDATE COMMITS:
{json.dumps(slim_commits, indent=1)[:40000]}

Reply with this JSON shape and nothing else:
{{
  "links": [
    {{"entry": "<entry id, copied exactly>",
      "claim": "<the delivered line, copied exactly>",
      "excerpt": "<candidate file id, or null>",
      "commit": "<candidate commit sha, or null>"}}
  ]
}}"""


def _compose_prompt(briefs: list[dict[str, Any]], window: dict[str, str]) -> str:
    return f"""{PIPELINE_MARKER}
You are composing an engineering journal for {window['since'][:10]} to {window['until'][:10]}.

Below are per-session briefs, each already reduced from a raw transcript.
Merge them into journal entries. One entry = one thread of work, which may span
several sessions (resumed work, the same task next morning, a git worktree and
its main tree). Do not emit one entry per session.

Audience: someone who did not do the work and does not read code. Founders,
managers, clients. Write like a weekly update, not a commit log.

Rules:
- Plain language. No file paths, function names or flags in prose. If a
  technical detail is genuinely needed, put it in `snippets`.
- Every entry cites the session ids it came from.
- A blocker MUST carry its resolution. If a brief reports something that failed
  with no resolution in the data, drop it rather than inventing one.
- `decisions` is what was settled on and why — the thing worth remembering.
- Copy `milestones` through as `timeline`, keeping each item's `goal` id
  exactly as given. Do not invent timestamps; the goal id supplies the time.
- Never mention future work, next steps, or recommendations.
- If a session contributed nothing worth journaling, list it in `skipped`.

BRIEFS:
{json.dumps(briefs, indent=1)[:400000]}

Reply with this JSON shape and nothing else:
{{
  "entries": [
    {{
      "id": "entry:short-slug",
      "title": "short plain-language title",
      "summary": "one or two sentences a non-technical reader understands",
      "kind": "code|docs|content|infra|config",
      "status": "landed|in-progress|abandoned",
      "project": "project name this belongs to",
      "sessions": ["session id"],
      "delivered": ["what now exists, plainly"],
      "decisions": ["what was settled on and why"],
      "blockers": [{{"what": "...", "resolution": "..."}}],
      "timeline": [{{"goal": "<goal id, copied exactly>", "what": "short beat"}}],
      "snippets": [{{"caption": "...", "code": "..."}}]
    }}
  ],
  "skipped": [{{"session": "id", "why": "reason"}}]
}}"""


def _resolve_timeline(
    entries: list[dict[str, Any]],
    goal_time: dict[str, str],
    session_date: dict[str, str] | None = None,
    known_projects: set[str] | None = None,
) -> int:
    """Attach real timestamps to timeline beats, by goal id.

    The model names the beat; the transcript supplies the time. A beat citing a
    goal that does not exist is dropped rather than shown at a guessed hour.
    """
    dropped = 0
    for entry in entries:
        beats: list[dict[str, Any]] = []
        for beat in entry.get("timeline") or []:
            if not isinstance(beat, dict):
                continue
            stamp = goal_time.get(str(beat.get("goal")))
            if not stamp:
                dropped += 1
                continue
            beats.append({"ts": stamp, "what": beat.get("what", ""), "goal": beat["goal"]})
        beats.sort(key=lambda b: b["ts"])
        entry["timeline"] = beats

        # The entry's date is when the work happened, not when a model said so.
        if beats:
            entry["date"] = beats[-1]["ts"][:10]
        elif not entry.get("date") and session_date:
            # No timeline: fall back to the last day its sessions were active,
            # so the entry never lands in an "undated" bucket.
            days = [
                session_date[s] for s in entry.get("sessions") or [] if s in session_date
            ]
            entry["date"] = max(days) if days else ""
        entry.setdefault("date", "")

        # Projects are the top-level aggregate, so collapse any sub-path the
        # model invented ("poslulu/mobile-shell") back onto a known project.
        project = str(entry.get("project") or "").strip().strip("/")
        if known_projects and project not in known_projects:
            head = project.split("/")[0]
            match = next(
                (p for p in known_projects if p == head or project.startswith(p)), ""
            )
            project = match or head or project
        entry["project"] = project or "unknown"
    return dropped


def _apply_links(
    entries: list[dict[str, Any]],
    links: list[dict[str, Any]],
    excerpts: dict[str, dict[str, Any]],
    commits: dict[str, dict[str, Any]],
) -> int:
    """Resolve link ids to real files, real code and real commits.

    The model chooses which claim an id belongs to; the id's *content* comes
    from disk. It cannot invent a path or paste code that was never written.
    """
    by_id = {(e.get("id") or e.get("title")): e for e in entries}
    attached = 0
    for link in links:
        entry = by_id.get(link.get("entry"))
        if not entry:
            continue
        excerpt = excerpts.get(str(link.get("excerpt")))
        commit = commits.get(str(link.get("commit")))
        if not excerpt and not commit:
            continue
        reference: dict[str, Any] = {"claim": link.get("claim", "")}
        if excerpt:
            reference["file"] = excerpt["file"]
            reference["code"] = excerpt["code"]
        if commit:
            reference["commit"] = commit["short"]
            reference["subject"] = commit["subject"]
        entry.setdefault("references", []).append(reference)
        attached += 1
    return attached


def _project_of(selection: Any, repos: dict[str, dict[str, Any]]) -> str:
    """Top-level project name: the git repo when there is one, else the folder.

    Projects are the primary aggregate, so `poslulu/mobile-shell` and
    `poslulu` must collapse to one bucket rather than reading as two.
    """
    cwd = selection.facts.cwd or ""
    for repo in repos.values():
        try:
            if cwd and Path(cwd).is_relative_to(Path(repo["root"])):
                return repo["name"]
        except (OSError, ValueError):
            continue
    area = selection.display_dir
    return area.split("/")[0].lstrip("~").strip() or area


def run_pipeline(
    selections: list,
    store: Store,
    window: dict[str, str],
    roots: list[str],
    console: Console,
    *,
    dry_run: bool = False,
) -> Path | None:
    """Run every stage over the picked sessions and return the journal path."""
    journal_id = datetime.now(timezone.utc).strftime("journal-%Y%m%dT%H%M%SZ")
    journal_dir = store.root / "journals" / journal_id
    digest_dir = store.root / "digest"
    store.ensure()

    # -- DISTILL ---------------------------------------------------------
    console.stage("DISTILL", "transcripts → action trees")
    trees: list[tuple[Any, list, dict, list]] = []
    for selection in selections:
        source = Path(selection.stored_path or selection.facts.source_path)
        nodes = build_tree(source)
        goals = prune(segment(nodes))
        summary = stats(nodes, goals)
        rows = excerpts(nodes, selection.facts.session_id)
        trees.append((selection, goals, summary, rows))
        share = summary["abandoned"] / max(1, summary["nodes"])
        console.bar(
            f"{selection.facts.session_id[:8]} {selection.display_dir.split('/')[-1][:9]}",
            share,
            f"{summary['nodes']}n {share*100:.0f}%",
            cells=8,
        )
    console.done(f"{len(trees)} sessions")

    # -- SEGMENT ---------------------------------------------------------
    console.stage("SEGMENT", "goals → attempts → outcomes")
    digest_dir.mkdir(parents=True, exist_ok=True)
    payloads: list[tuple[Any, list[dict]]] = []
    goal_time: dict[str, str] = {}
    all_excerpts: dict[str, dict[str, Any]] = {}
    for selection, goals, _, rows in trees:
        payload = [g.to_dict() for g in goals]
        for goal in goals:
            if goal.ts:
                goal_time[goal.id] = goal.ts
        for row in rows:
            all_excerpts[row["id"]] = row
        if not dry_run:
            (digest_dir / f"{selection.facts.session_id}.actions.json").write_text(
                json.dumps(payload, indent=1), encoding="utf-8"
            )
        payloads.append((selection, payload))
        size = len(json.dumps(payload))
        console.step(
            f"{selection.facts.session_id[:8]}  {len(payload):>3} goals  "
            f"{size/1024:6.0f} KB  ~{size/4/1000:.0f}k tokens"
        )
    console.done(f"digests written to {digest_dir.name}/")

    # -- GIT -------------------------------------------------------------
    console.stage("GIT", "corroborating what actually landed")
    since = parse_timestamp(window["since"]) or datetime.now(timezone.utc)
    until = parse_timestamp(window["until"]) or datetime.now(timezone.utc)
    survey_paths = []
    for selection in selections:
        if selection.facts.cwd:
            survey_paths.append(Path(selection.facts.cwd))
    repos = git.survey(survey_paths, since, until)
    all_commits: dict[str, dict[str, Any]] = {}
    for repo in repos.values():
        for commit in repo["commits"]:
            all_commits[commit["short"]] = commit
        console.step(
            f"{repo['name']:<22}{len(repo['commits']):>3} commits  "
            f"{'on ' + repo['branch'] if repo['branch'] else ''}"
        )
    if not repos:
        console.detail("no git repositories among the picked sessions")
    console.done(f"{len(all_commits)} commits in window")

    if dry_run:
        console.warn("dry run: stopping before any model call")
        return None

    # -- BRIEF -----------------------------------------------------------
    console.stage("BRIEF", "each session → what was done")
    briefs: list[dict[str, Any]] = []
    model, effort = llm.MECHANICAL
    for selection, payload in payloads:
        facts = selection.facts
        console.model(model, effort, f"session {facts.session_id[:8]}")
        console.stream_open()
        try:
            reply = llm.run(
                _brief_prompt(facts.to_dict(), payload),
                model=model,
                effort=effort,
                system=BRIEF_SYSTEM,
                on_text=console.stream_text,
                cwd=str(store.agent_dir),
            )
            console.stream_close(reply)
            brief = llm.extract_json(reply.text)
        except llm.LLMError as error:
            console.stream_close()
            console.error(f"{facts.session_id[:8]}: {error}")
            continue
        if isinstance(brief, dict):
            brief["session"] = facts.session_id
            brief["title"] = facts.title
            brief["project"] = _project_of(selection, repos)
            brief["area"] = selection.display_dir
            brief["date"] = selection.last_active
            briefs.append(brief)
    if not briefs:
        console.error("no session briefs produced; stopping")
        return None
    console.done(f"{len(briefs)} briefs")

    # -- COMPOSE ---------------------------------------------------------
    console.stage("COMPOSE", "briefs → journal entries")
    model, effort = llm.SYNTHESIS
    console.model(model, effort, f"{len(briefs)} briefs")
    console.stream_open()
    try:
        reply = llm.run(
            _compose_prompt(briefs, window),
            model=model,
            effort=effort,
            system=COMPOSE_SYSTEM,
            on_text=console.stream_text,
            cwd=str(store.agent_dir),
        )
        console.stream_close(reply)
        composed = llm.extract_json(reply.text)
    except llm.LLMError as error:
        console.stream_close()
        console.error(str(error))
        return None
    if not isinstance(composed, dict) or "entries" not in composed:
        console.error("compose did not return an entries object")
        return None

    entries = composed.get("entries") or []
    session_date = {s.facts.session_id: s.last_active for s, _ in payloads}
    known_projects = {b["project"] for b in briefs}
    dropped = _resolve_timeline(entries, goal_time, session_date, known_projects)
    if dropped:
        console.warn(f"{dropped} timeline beats cited an unknown goal — dropped")
    console.done(f"{len(entries)} entries · {len(composed.get('skipped') or [])} skipped")

    # -- LINK ------------------------------------------------------------
    console.stage("LINK", "attaching code and commits to claims")
    attached = 0
    if entries and (all_excerpts or all_commits):
        model, effort = llm.MECHANICAL
        console.model(model, effort, f"{len(all_excerpts)} files · {len(all_commits)} commits")
        console.stream_open()
        try:
            reply = llm.run(
                _link_prompt(entries, list(all_excerpts.values()), list(all_commits.values())),
                model=model,
                effort=effort,
                system=LINK_SYSTEM,
                on_text=console.stream_text,
                cwd=str(store.agent_dir),
            )
            console.stream_close(reply)
            linked = llm.extract_json(reply.text)
            rows = linked.get("links") if isinstance(linked, dict) else linked
            journal_dir.mkdir(parents=True, exist_ok=True)
            (journal_dir / "links.json").write_text(
                json.dumps(
                    {
                        "candidates": {
                            "excerpts": list(all_excerpts),
                            "commits": list(all_commits),
                        },
                        "entry_ids": [e.get("id") for e in entries],
                        "returned": rows,
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
            if isinstance(rows, list):
                attached = _apply_links(entries, rows, all_excerpts, all_commits)
            else:
                console.warn(f"link pass returned {type(rows).__name__}, expected a list")
        except llm.LLMError as error:
            console.stream_close()
            console.warn(f"link pass skipped: {error}")
    else:
        console.detail("nothing to link against")
    console.done(f"{attached} references attached")

    # -- RENDER ----------------------------------------------------------
    console.stage("RENDER", "entries → markdown + html")
    journal_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "journal_id": journal_id,
        "generated_at": isoformat(datetime.now(timezone.utc)),
        "window": window,
        "roots": roots,
        "sessions": [s.facts.session_id for s, _ in payloads],
        "repos": [
            {"name": r["name"], "branch": r["branch"], "commits": len(r["commits"])}
            for r in repos.values()
        ],
    }
    (journal_dir / "entries.json").write_text(
        json.dumps({"meta": meta, **composed}, indent=2), encoding="utf-8"
    )
    (journal_dir / "briefs.json").write_text(
        json.dumps(briefs, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(composed, meta)
    (journal_dir / "journal.md").write_text(markdown, encoding="utf-8")
    html_path = journal_dir / "journal.html"
    html_path.write_text(render_html(composed, meta), encoding="utf-8")
    console.step(f"journal.md    {len(markdown.splitlines()):>5} lines")
    console.step(f"journal.html  {html_path.stat().st_size/1024:>5.0f} KB  self-contained")
    console.done()

    console.finish(
        str(html_path),
        {
            "entries": len(entries),
            "projects": len({e.get("project") for e in entries}),
            "sessions": len(payloads),
            "beats": sum(len(e.get("timeline") or []) for e in entries),
            "refs": attached,
        },
    )
    return html_path
