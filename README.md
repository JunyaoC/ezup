# ez-change-log

Turn Claude Code session history into changelogs and reports.

Point it at directories, give it a time window, tick the sessions you care
about, and it writes a journal: *worked on · delivered · tried · worked ·
didn't work*, every bullet traceable to the session it came from.

No Python dependencies (3.10+). Model work shells out to `claude -p`, so it
uses your existing Claude Code auth — no API key, no SDK. Setup is one script.

## The pipeline

```
COLLECT   code            pick sessions by directory + window
DISTILL   code            transcript → action tree (trunk / abandoned) + code excerpts
SEGMENT   code            tree → goals → attempts → outcomes
GIT       code            commits in the window, per repository
BRIEF     sonnet · low    each session → what was done, decided, blocked
COMPOSE   opus  · max     all briefs → journal entries
LINK      sonnet · low    attach real code and commits to each claim
RENDER    code            entries → journal.md + journal.html
```

The terminal runs as a single live frame — title, stages on the left, model
stream on the right, run settings underneath — redrawn in place by a ticker so
the spinner, clock and token counter keep moving while a model thinks:

```
  ────────────────────────────────────────────────────────────────────────
  ✔ DISTILL  2 sessions 0.8s      ┌─▂▄▆▆▄─ sonnet/low · 186 tok · 3.1s ──┐
  ✔ SEGMENT  77 goals 0.3s        │  "delivered": ["per-client switch…    │
  ⠧ BRIEF      3.1s               │  "delivered": ["dedicated driver…     │
    each session → what was done  │                                       │
  · COMPOSE                       └───────────────────────────────────────┘
  ────────────────────────────────────────────────────────────────────────
  ez-change-log, aiyu-stack   ·   store ~/.ezchangelog
```

## Reading the journal

The axis you group by becomes the page's `h1`; the other axis nests inside it
as `h2`. Group by **project** and you read projects containing days; group by
**date** and you read days containing projects. Projects are the default and
are set large, since that is how the work is actually owned.

Three of the six stages are plain code. A transcript is already a DAG — every
entry carries `parentUuid` — so branch points are the moments you rewound and
tried something else, and they are *recovered*, not inferred. On real sessions
50–75% of nodes sit on abandoned branches. That is what fills "tried" and
"didn't work", and it is why the model is never asked to guess what failed.

It also makes the whole thing affordable: a 37 MB transcript reduces to a
194 KB digest before any model sees it. A two-session journal costs about
$0.30 and takes under a minute. Only COMPOSE forms judgment, so only COMPOSE
gets Opus at max effort; everything else is Sonnet at low effort or free.

Every model call is stateless — a fresh `claude -p`, no `--continue`, no
`--resume`, nothing shared between stages except files on disk.

## Evidence, not assertion

**LINK** attaches the code behind a claim. The model only ever picks an id from
a candidate list; the file path and the code itself are resolved from the
transcript afterwards. It cannot invent a filename or paste code that was never
written. Same rule as the timeline: the model names the beat, the transcript
supplies the timestamp, and a beat citing an unknown goal is dropped.

**GIT** reads commits in the window per repository, so an entry can cite what
actually landed rather than only what was attempted. Projects with no
repository degrade quietly.

Projects are the top-level aggregate — the git repo name when there is one, so
`poslulu/mobile-shell` and `poslulu` collapse into one project rather than
reading as two.

## Setup

On a fresh machine:

```bash
git clone <this repo> && cd ez-change-log
./setup.sh
```

That installs [uv](https://docs.astral.sh/uv/) if it is missing, creates
`.venv` on Python 3.11, installs this package into it as editable, checks for
the `claude` CLI the pipeline shells out to, and smoke-tests the result. It is
idempotent — re-run it any time to verify a machine.

`./collect` picks up `.venv` automatically. If you would rather have `ezcl` on
your PATH directly:

```bash
source .venv/bin/activate
ezcl collect --since 7d
```

There are no third-party dependencies; the venv exists for the entry point and
a pinned interpreter, not for packages. If you skip setup entirely, `./collect`
falls back to system `python3` and still works.

## Team sharing (ezupdate) — as a Claude Code plugin

The dev-side hook ships as a plugin. Install it from this repo:

```
/plugin marketplace add JunyaoC/ez-change-log
/plugin install ezupdate@ez-change-log
```

Then, inside any session you want to share:

```
/ezupdate on        # this session only; nothing before now is sent
/ezupdate status    # where bytes go, and why sharing is on or off
/ezupdate off
```

The plugin is **inert without consent**: installing it shares nothing, and if
`ezcl` is not on PATH it does nothing at all (`./setup.sh` links it into
`~/.local/bin`). Prefer no plugin? `ezcl hook install` wires the same hooks
into settings.json directly — use one or the other, not both.

## Start here

```bash
./collect                          # every project, last 7 days
./collect 30d                      # every project, last 30 days
./collect 2026-08-01               # every project, since that date
./collect 2026-08-01..2026-08-07   # every project, that date range
./collect ~/code/proj              # one project, last 7 days
./collect ~/code/proj 30d          # one project, last 30 days
```

The directory is optional — with none, you get every Claude session in the
window. The window accepts a duration (`7d`, `24h`, `2w`), a date, or a
`start..end` range; bare dates are local, and an end date includes that whole
day. Directory and window can appear in either order.

`collect` is the only entry point. It lists every session in the window, you
tick the ones you want, and the pipeline runs straight through to a journal —
printing each stage as it goes and finishing with the path to a self-contained
HTML file. Any flag can be appended (`./collect ~/code/proj 30d --no-journal`).
The script works from any directory — no install, no `PYTHONPATH` to set.

```
[1/5] DISTILL transcript → action tree
      76cd5aeb   37.0 MB   2065 nodes  188 directives   780 actions
                120 goals  74.5% abandoned  24 failed calls  27 files edited
[2/5] SEGMENT goals → attempts → outcomes
      76cd5aeb  120 goals     194 KB digest  (~50k tokens)
[3/5] BRIEF   claude -p --model sonnet --effort low   session 76cd5aeb
[4/5] COMPOSE claude -p --model opus --effort max     2 briefs → journal entries
[5/5] RENDER  entries → markdown + html

journal ready in 46.4s  ·  $0.2940
  ~/.ezchangelog/journals/journal-20260811T015501Z/journal.html
```

| flag | effect |
| --- | --- |
| `--no-journal` | collect only, skip the pipeline |
| `--stop-before-model` | run distill + segment, stop before spending anything |
| `--quiet` | stage lines only; don't stream model output |

## Full CLI

```bash
# the usual loop: last 7 days, review the list, pick what belongs in the changelog
python3 -m ezchangelog collect ~/code/proj-a -i

python3 -m ezchangelog collect ~/code/proj-a ~/code/proj-b --since 7d
python3 -m ezchangelog collect ~/code/proj-a --since 30d --dry-run
python3 -m ezchangelog collect ~/code/proj-a --since 7d --json > window.json
python3 -m ezchangelog status
```

You get a checkbox menu of every session in the window:

```
 9 sessions   3 selected   [space] toggle  [a] all  [n] none  [enter] collect  [q] cancel
[x] 2026-08-10 10:19 cwd       87t    77x  lab/ez-change-log     Build CLI tool to scrape sessions
[ ] 2026-08-10 07:11 cwd     1145t  1075x  lab/aiyu-stack        UI revamp scope and IA review
[x] 2026-08-06 13:01 cwd       29t    24x  lab                   Update Tailscale to latest version
[ ] 2026-08-05 15:03 cwd        2t     0x  lab/atama/v2/tui      b0ff110d
```

| key | action |
| --- | --- |
| `space` | toggle the row and move down |
| `↑`/`↓` or `k`/`j` | move |
| `PgUp`/`PgDn`, `g`/`G` | page, jump to top/bottom |
| `a` / `n` | select all / none |
| `enter` | collect the checked sessions |
| `q` or `esc` | cancel — nothing is written, exits 1 |

Each row shows the directory the session ran in (relative to the root you
passed), why it matched, its user turns (`t`) and tool calls (`x`), and its
title — or the session id when it has no title.

When stdout or stdin is redirected the menu is replaced by a numbered prompt
that accepts `1,3,5-8`, `a`, `n`, so the picker still works in a pipe.

`ezcl` is the same tool without the launcher's argument sugar.

### collect

| flag | meaning |
| --- | --- |
| `--since` | `7d`, `24h`, `2w`, or an ISO-8601 instant. Default `7d`. |
| `--until` | End of window. Default now. |
| `-i`, `--interactive` | List the window's sessions and pick which to keep. |
| `--match` | `cwd`, `touched`, or `any`. Default `any`. |
| `--no-recursive` | Match only the exact directories, not their subdirectories. |
| `--min-turns N` | Drop sessions under N real user prompts. Default 1. |
| `--min-tools N` | Drop sessions under N tool calls. Default 1 — a session that ran nothing changed nothing. `0` keeps talk-only sessions. |
| `--dry-run` | Report the selection without writing anything. |
| `--refresh` | Re-parse every transcript, ignoring cached index metadata. |
| `--limit N` | Rows to print (0 = all). Display only; does not affect what is stored. |
| `--json` | Emit the run manifest to stdout. |

## What gets filtered out

Three kinds of session are dropped before you ever see the list:

- **The pipeline's own model calls.** Every `claude -p` writes its own
  transcript into `~/.claude/projects`, so without a guard the tool journals
  itself journaling. Calls run from `~/.ezchangelog/.agent` and carry an
  `[ezchangelog-pipeline]` marker; either one excludes them.
- **Sessions that did nothing** — no real prompt, or no tool call at all.
- **Harness bookkeeping.** Slash-command echoes, their stdout, and
  `<local-command-caveat>` blocks are not user turns, so a session containing
  only those counts as empty.

`TURNS` counts real prompts you typed, not tool results returning to the model.

## The store

Default `~/.ezchangelog`, overridable with `--store` or `$EZCHANGELOG_HOME`.

```
~/.ezchangelog/
  index.json                             pointers + ingest state
  raw/<project-slug>/<sessionId>.jsonl   verbatim copy of the transcript
  runs/<run-id>.json                     manifest of one collect run
```

Raw copies are byte-identical to the source transcripts — full-fidelity
passthrough, nothing filtered. Budget for it: 250 sessions ran about 176 MB.

`index.json` is what makes repeat scrapes cheap. Each source transcript is
keyed by its path and stamped with `(size, mtime_ns)`. If that signature is
unchanged since the last run, the file is never reopened and its cached facts
are reused. A cold scan of 1180 transcripts takes ~3s; a warm one ~0.2s.

Each run manifest lists the selected sessions with their mechanical facts:
`cwd`, git branches, AI title, first/last timestamp, entry-type histogram,
user/assistant turn counts, sidechain count, tool-use histogram, and the
filesystem paths touched by tool calls.

## How sessions are matched to directories

Claude stores transcripts under `~/.claude/projects/<slug>/<sessionId>.jsonl`,
where the slug encodes the project path with `/` and `.` both flattened to `-`.
That encoding is lossy and **not reversible** — `-Users-me-lab-atama-v2-tui`
could mean `/lab/atama/v2/tui` or `/lab/atama-v2/tui`, and prefix-matching a
slug would also sweep in unrelated siblings.

So matching ignores slugs entirely and works on real, resolved path containment
against the directories you passed. Subdirectories are included at any depth.

Sessions are often started somewhere other than the folder they end up working
on — you open Claude in `~/code`, then spend the session editing `~/code/app`.
Matching on `cwd` alone would miss that, so two kinds of evidence are used, and
the `WHY` column tells you which one fired:

| reason | meaning |
| --- | --- |
| `cwd` | the session was started inside the directory |
| `edited` | the session wrote files inside it (`Edit`/`Write`/`NotebookEdit`) |
| `read` | the session only read files inside it |

`--match any` (default) accepts all three. `--match cwd` is the strict, original
behaviour. `--match touched` ignores where the session started and asks only
which files it handled.

`read` is the weakest evidence and the most likely to be noise — a session that
merely opened one file in your project. That is what `-i` is for: the reason is
on screen, so you decide.

## Time window

A session is selected when it was **worked on** during the window — not merely
when its lifetime overlaps it. Each session records the local dates that carry
entries, and selection tests those days.

The distinction is not academic: one real session opened on 29 June and was
last touched on 10 August. Overlap logic drags it into every window in between,
including weeks when it was never opened. Day-based selection does not.

The `ACTIVE` column shows the last day of work **inside the window**, with
`+Nd` when the session was worked on more days than one. The stored copy is
always the whole transcript, so a session straddling the boundary keeps its
full context.
