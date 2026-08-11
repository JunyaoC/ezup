# Phase 2 — from raw sessions to a journal

Recon, scope and design for turning collected Claude sessions into an
evidence-anchored changelog/journal. **No implementation yet.**

Phase 1 (`./collect`) is the substrate: raw transcripts in `~/.ezchangelog/raw/`
plus a run manifest listing what fell in the window. This document designs what
happens after that.

---

## 1. Recon — what discovery-kit does

`../discovery-kit/` turns a recorded working session into an evidence-anchored
report website. Its shape, from `docs/DISCOVERY-PROTOCOL.md` and
`docs/DISCOVERY-REPRESENTATION.md`:

**Pipeline.** `INTERVIEW → ORIENT → OBSERVE → SYNTHESIZE → REPRESENT → AUDIT`,
each stage a separate agent run, each handoff a durable artifact on disk.

**Evidence spine.** The load-bearing invariant. Three tiers:

```
verbatim quote  →  atom  →  claim  →  rendered element
```

An atom (`observe.atoms.json`, 690 of them in the reference run) is
`{id, type, claim, verbatim, t, speaker, entities[], epistemic, affect, source_check}`.
A claim (`synthesize.ledger.json`, 105 of them) is
`{id, statement, atoms[], recurrence, reliability, confidence, entities[]}`.
Nothing renders without a path back to a quote.

**Principles worth stealing verbatim** (protocol §"Cross-cutting principles"):

| # | Principle | Why it matters here |
|---|---|---|
| 1 | One evidence spine | A changelog line nobody can trace is a rumour |
| 2 | Three walls: observe ≠ infer ≠ solve | Stops "what we did" drifting into "what we should do" |
| 4 | Recall flows up, judgment flows down | Over-collect atoms; decide what's newsworthy later |
| 5 | Handoffs inspectable and idempotent | Re-running a stage yields the same shape |
| 7 | Stateless stages, fresh sessions | State lives in files, never in a conversation |
| 8 | Orient, then attack the first impression | The obvious story about a week is usually wrong |

**Representation principles worth stealing:** lead with the synthesis and keep
the spine one click away; every rendered element is claim-labeled; render,
never invent — and *name the absence* where evidence is missing; two audiences,
one truth; portable and self-contained.

### What does *not* transfer

Cargo-culting the whole kit would be a mistake. Four real differences:

1. **Their atoms are speech; ours are structured.** Extracting an atom from
   audio needs ASR, diarization and an LLM. Extracting one from a transcript is
   a JSON field read. **Our observe stage should be deterministic code, not an
   agent.** This is the single biggest divergence and it's in our favour.
2. **They analyze one recording; we analyze N sessions across M projects and a
   time window.** We inherit a merge axis they don't have: resumed sessions,
   git worktrees, subagent sidechains, and the same work continuing across days.
   Dedup is our hard problem, not evidence extraction.
3. **"No solutioning" becomes "no roadmap".** Their third wall forbids
   recommendations because discovery is strictly current-state. Ours forbids
   speculation about *intent* and *next steps*: the journal reports what
   changed, not what it means for the roadmap.
4. **Their site is an Astro app with React Flow.** We want one portable HTML
   file. Their template is a floor, not a ceiling — we're building a different,
   smaller thing.

---

## 2. The constraint that decides the architecture

Measured on a real 4-session collect run:

| | raw | as atoms |
|---|---|---|
| 4 sessions | 71.7 MB | — |
| tokens | **~17.9 M** | **~162 K** |

Two sessions alone were 34 MB and 37 MB (6436 and 5284 entries). Feeding raw
transcripts to a model is not merely expensive, it is impossible — one session
exceeds any context window.

**Therefore: a deterministic distillation stage is not an optimization, it is
the precondition for the pipeline existing at all.** Roughly a 110× reduction,
with zero model cost and zero hallucination risk, before any agent is invoked.
Every design decision below follows from this.

---

## 3. Scope

### In

- Deterministic reconstruction of each session's **action tree** — goals,
  attempts, outcomes, and which paths were abandoned.
- Grouping goals into **entries** — the unit of a changelog line — across
  sessions, days and worktrees.
- A bullet markdown journal covering *worked on · delivered · tried · worked ·
  didn't work*, every bullet traceable to a node in the tree.
- Classification of each entry: code / docs / content / infra / config.
- One self-contained `journal.html`: all CSS, JS and data inlined, opens from
  the filesystem, survives being moved or emailed.
- Three organizations of the same entries: **chronological**, **by project**,
  **by change kind** — switchable in the page, not three separate builds.
- Two reading levels: **release notes** (plain) and **engineering journal**
  (paths, commands, prompts), same entry set.
- A markdown mirror for pasting into a PR, README or release.

### Out (for the first cut)

- Publishing/hosting, auth, deploy scripts.
- Reading git history. Sessions are the source; git is a later corroboration
  source, and mixing them now doubles the design surface.
- Diff rendering of file contents. `file-history-snapshot` entries are
  captured as evidence but shown as *what changed*, not as a rendered diff.
- Multi-user / team aggregation.
- Incremental journal updates (append a day to an existing journal). Journals
  are regenerated whole; they're cheap once atoms are cached.

---

## 4. The pipeline

Mirrors discovery-kit's stage/contract discipline, with the agent boundary
moved: stages 0–1 are code, stages 2–4 are agent work over small artifacts.

```
   ./collect             COLLECT     code    raw/<slug>/<session>.jsonl
        ↓                                    runs/<run-id>.json
   ezcl distill          DISTILL     code    digest/<session>.tree.json
        ↓                                       nodes + lane(trunk|abandoned)
   ezcl segment          SEGMENT     code    digest/<session>.actions.json
        ↓                                       goals → attempts → outcomes
   /journal-orient       ORIENT      agent   <journal>/orientation.md
        ↓
   /journal-compose      SYNTHESIZE  agent   <journal>/entries.json
        ↓                                       delivered[] / tried[]
   /journal-render       REPRESENT   agent   <journal>/journal.md + .html
        ↓
   ezcl audit            AUDIT       code    <journal>/audit.json
```

The first three stages are code. The tree, the abandoned branches and the
failures are **facts recovered from the file**, not inferences — so the model
never has to guess what was tried, only explain it.

### Stage 1 — DISTILL (deterministic)

One transcript in, one **action tree** out. Cached against the same
`(size, mtime_ns)` signature the phase-1 index already uses, so re-distilling a
window is free.

**The tree is not invented — it is already in the file.** Every entry carries
`parentUuid`, so a transcript is literally a DAG, not a list. Measured on four
real sessions:

| session | nodes | branch points | max fanout | tool_results | errors |
|---|---|---|---|---|---|
| `5eba26ea` | 6018 | 27 | 6 | 1341 | 11 |
| `76cd5aeb` | 3882 | 61 | 3 | 780 | 24 |
| `082d912d` | 4936 | 11 | 2 | 1075 | 22 |
| `6806a69b` | 5875 | 25 | 3 | 1199 | 25 |

A branch point is a rewind: the moment you backed up and tried something else.
In `082d912d`, **2661 of 4936 nodes are off the surviving path** — 54% of that
session is roads not taken, and it is all still on disk. A real example, two
siblings of one parent:

```
'make the menu to be a checkbox isntead?'
'make the menu to be a checkbox isntead? in front of the item first column'
```

That is intent being sharpened, recorded verbatim. A flat list of edits throws
it away; the tree keeps it.

**Node types:**

| type | from | carries |
|---|---|---|
| `directive` | `user` entry | the verbatim prompt — the goal being set |
| `action` | any `tool_use` | tool, target path(s) or command, verbatim input |
| `outcome` | matching `tool_result` | `ok` \| `error`, and the error text |
| `snapshot` | `file-history-snapshot` | path + real before/after file state |
| `note` | assistant text adjacent to an action | the sentence explaining it |

Every node: `{id, parent, type, ts, cwd, git_branch, paths[], verbatim,
session_id, is_sidechain, lane}`.

**Two derived labels, both mechanical:**

- `lane: trunk | abandoned` — is this node on the path that survived to the end
  of the session, or on a branch that was rewound away? *(Care needed: the
  `last-prompt` `leafUuid` marks the last prompt's leaf, not the final node, so
  the trunk is better defined as the path to the chronologically last entry.
  Getting this rule right is a task, not a solved problem.)*
- `outcome` — `error` when the tool result says `is_error`, plus the softer
  signals: a file edited then edited again within N turns, a command re-run
  after a failure, a user turn that opens with a correction.

**Wall:** this stage may not summarize or judge. It shapes and labels what is
already there; it never writes a sentence that isn't in the transcript.

### Stage 1.5 — SEGMENT (deterministic)

Cut the tree into **goals and attempts** — still no model involved.

- A `directive` opens a **goal**. Its subtree, up to the next directive, is the
  work done for that goal.
- Within a goal, an **attempt** is a run of actions ending in an outcome:
  a branch that got abandoned, a command that failed then succeeded, an edit
  reverted and redone.
- Attempts inherit `lane`, so an abandoned attempt is identifiable without a
  model guessing at it.

Output: `digest/<session>.actions.json` — `{goals: [{directive, attempts: [...]}]}`.
This is the artifact the agent stages actually read, and it is small.

### Stage 2 — ORIENT (agent, cheap)

Reads only the atom *index* — counts, paths, titles, timestamps — never
verbatims in bulk. Produces a provisional shape of the window: which projects
moved, roughly what happened, what the expected story is.

Per discovery-kit principle 8, this is a **hypothesis to attack**, not a
conclusion. Written to `orientation.md`, never rendered into the journal.

### Stage 3 — SYNTHESIZE (agent)

The hard stage, and where our merge axis lives. Groups goals into **entries** —
each one a thread of work, carrying its trajectory rather than just its result.

```jsonc
{
  "id": "entry:collect-cli-incremental-index",
  "title": "Incremental session index",
  "summary": "Repeat scrapes reuse cached metadata instead of re-reading every transcript.",
  "kind": "code",              // code | docs | content | infra | config
  "status": "landed",          // landed | in-progress | abandoned
  "projects": ["ez-change-log"],
  "paths": ["ezchangelog/store.py", "ezchangelog/collect.py"],
  "sessions": ["c7543655-…"],
  "goals": ["c7543655-…#g03"],
  "delivered": [               // survived to the end, with evidence
    {"what": "stat-signature cache keyed by (size, mtime_ns)",
     "evidence": ["c7543655-…#n214"]}
  ],
  "tried": [                   // attempted; kept or not, both are the record
    {"what": "slug prefix matching to find a project's sessions",
     "outcome": "rejected",
     "why": "slug is lossy — /lab/atama/v2/tui and /lab/atama-v2/tui collide",
     "evidence": ["c7543655-…#n031"]}
  ],
  "first_ts": "2026-08-10T08:54:39Z",
  "last_ts": "2026-08-10T09:05:24Z",
  "confidence": 0.9
}
```

`delivered` and `tried` are the whole point. `tried` is populated from
`lane: abandoned` attempts and failed outcomes — the model's job is to *explain*
them, not to discover them, because the tree already knows which paths died.

Three grouping problems, in order of difficulty:

1. **Within a session** — many edits to one concern = one entry. Signals:
   shared path prefix, temporal adjacency, the directive that preceded them.
2. **Across sessions** — a resumed session, or the same work next morning.
   Signals: overlapping `paths`, same project, adjacent days. This is what
   makes the journal read as work rather than as a session log.
3. **Across worktrees** — `atama/cahaya/state/worktrees/e625-2` and the main
   tree are the same change. Signals: identical path suffix after the worktree
   root, same branch name. Phase 1 already records `git_branches` per session
   for exactly this.

**Walls:** an entry with no nodes behind it does not exist. `status` and
`outcome` are read from evidence (did a `snapshot` land? did the command exit
clean? was the branch abandoned?) and never inferred from optimism. No entry may
state intent the user never expressed, and none may mention future work.

### Stage 4 — REPRESENT

Renders `journal.html` and `journal.md` from `entries.json` + atoms. Detailed
in §5.

### Stage 5 — AUDIT (deterministic)

The acceptance gate as code, not vibes. Fails the build when:

- an entry cites a node id that doesn't exist
- a node's `paths` don't fall under the entry's claimed project
- an entry has zero nodes
- a `landed` entry has no `action` or `snapshot` node behind it
- a "didn't work" bullet cites no `abandoned` lane and no `error` outcome —
  the model editorializing a failure the tree doesn't support
- the journal contains an entry id absent from `entries.json`
- any collected session appears in **no** entry and no explicit
  `excluded_sessions[]` reason — the "name the absence" rule, mechanized

---

## 5. The journal (representation)

### Why one HTML file

Portability is a discovery-kit invariant and it's the right call here: the
journal gets moved, attached, dropped in a shared folder. A single file with
inlined CSS/JS and a `<script type="application/json">` data island has no
build step, no server, no broken relative paths. Entries and their atoms are
small — the 4-session run distilled to ~4000 atoms — so the whole spine fits
in the page and every drill-down is instant and offline.

### Structure — conclusion first, spine one click away

```
┌───────────────────────────────────────────────────────────┐
│ Journal · lab · 4–11 Aug 2026        [notes|engineering]  │  reading level
│ 12 entries · 4 sessions · 3 projects                      │
├───────────────────────────────────────────────────────────┤
│ group by:  ( Date )  ( Project )  ( Kind )                │  re-groups in page
├───────────────────────────────────────────────────────────┤
│ ▾ Mon 10 Aug                                              │
│   ● Incremental session index          [code]  poslulu    │
│     Repeat scrapes reuse cached metadata…                 │
│     ▸ evidence (2 sessions · 14 atoms)                    │
│         "make repeat scrapes cheap"      ← directive      │
│         ✎ ezchangelog/store.py           ← edit           │
│         $ python3 -m ezchangelog status  ← command        │
└───────────────────────────────────────────────────────────┘
```

- **Group-by is a client-side re-render of one dataset.** Date, project and
  kind are three views of the same entries — not three builds, and not a
  decision made at generation time.
- **Reading level is a toggle over the same entry set.** Release notes hide
  paths, commands and prompts; the engineering view shows them. Identical ids
  and identical claims in both — discovery-kit's "two audiences, one truth".
- **Every entry expands to its atoms**, each atom showing its verbatim and its
  timestamp. That is the evidence spine reaching the surface.
- **Absence is named.** Sessions that produced no entry are listed in a
  "collected but not journaled" section with the reason, rather than silently
  dropped.
- **Kind is a visible chip**, because the whole point is that some weeks are
  code and some are docs/website.

### The bullet markdown

The primary written form. Five sections per group, driven directly by the
action tree — each answers a question the tree can evidence:

```markdown
## Mon 10 Aug · ez-change-log

**Worked on**
- Session scraping CLI — directory + time-window extraction into `~/.ezchangelog`

**Delivered**
- Incremental index: repeat scrapes reuse cached metadata (3.6s → 0.16s)
- Interactive checkbox picker for choosing sessions
- Directory matching by real `cwd` containment, recursive at any depth

**Tried**
- Slug prefix matching to find a project's sessions
- Matching sessions only by `cwd`

**Worked**
- `(size, mtime_ns)` signature as the cache key — 1180 transcripts, 0 re-reads
- Matching on `touched_paths` as well as `cwd` — found 2 sessions `cwd` missed

**Didn't work**
- Slug prefix matching — the slug is lossy, `/lab/atama/v2/tui` and
  `/lab/atama-v2/tui` produce the same string, and prefixes swept in siblings
- Arrow keys in the pty test — curses uses keypad-application mode (`ESC O B`)
```

Two rules keep this honest:

- **"Didn't work" is populated from `lane: abandoned` and `is_error`, not from
  the model's opinion.** A dead end that isn't in the tree doesn't get written.
- **Every bullet keeps a node id**, so the HTML view can expand it to the
  verbatim and the markdown can carry it as a footnote reference.

This is also the section set that makes the two reading levels natural:
release notes are *Delivered* only; the engineering journal is all five.

### HTML mirror

The same five sections, with each bullet expandable to its evidence and the
group-by / reading-level toggles from above.

---

## 6. Cost model

| stage | cost |
|---|---|
| COLLECT | free, ~3s cold / 0.2s warm |
| DISTILL + SEGMENT | free, deterministic, cached per transcript |
| ORIENT | goal index only — a few thousand tokens |
| SYNTHESIZE | goals + attempts for the window — ~162 K tokens for a 4-session week |
| REPRESENT | entries + template — small |

A week's journal costs roughly one large-context call, not 18 M tokens of
transcript. Distillation is what buys that.

---

## 7. Open decisions

1. **How much of the abandoned 54% earns a line?** Most rewinds are typos and
   restarts, not interesting dead ends. Needs a materiality rule — an abandoned
   branch counts as *tried* only if it contains an `action` (something was
   actually run or written), not merely a re-typed prompt. Recommend starting
   strict and loosening.
2. **Entry granularity.** One entry per concern (a dozen a week, reads like a
   changelog) or per session (one per session, reads like a log)? Recommend
   per concern — sessions ranged from 3 to 1145 turns in real data, so the
   session is not a meaningful unit of change.
3. **Trunk definition.** `last-prompt.leafUuid` marks the last *prompt's* leaf,
   not the session's final node — on `082d912d` that rule leaves both children
   of some branch points labelled abandoned. The trunk is probably the path to
   the chronologically last entry, but this needs to be settled against real
   sessions before anything depends on it.
2. **Do directives get quoted verbatim?** Your prompts are the clearest
   statement of intent, but they're informal and sometimes contain private
   context. Options: quote them in the engineering view only; paraphrase; or
   omit.
3. **Where do journals live** — `~/.ezchangelog/journals/<id>/` next to the
   raw store, or in the project being journaled?
4. **Agent boundary.** Stages 2–4 as Claude Code skills in `.claude/skills/`
   (discovery-kit's model — you run `/journal-compose` yourself and stay in the
   loop) or as API calls the CLI makes directly (one command, no interaction)?
