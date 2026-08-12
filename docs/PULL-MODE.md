# Pull mode — devs publish, the PM pulls

Scope for turning ez-changelog from a personal tool into a team one, without
asking anyone to write status updates.

Today the tool reads `~/.claude/projects` on **one** machine. Pull mode lets a
developer opt in with a hook, ships a **digest** of each session to a shared
place, and lets a PM collect from every developer and run the same pipeline
locally.

**Nothing here is built yet.** This is the scope.

---

## 1. What the hook contract actually gives us

Verified against the installed Claude Code binary (v2.1.227), not from memory.

**Events:** `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`,
`SessionEnd`, `Stop`, `SubagentStop`, `Notification`, `PreCompact`,
`PostCompact`.

**Payload on stdin (JSON):** `hook_event_name`, `session_id`,
**`transcript_path`**, `cwd`, `permission_mode`, `prompt`, `source`,
`tool_name`, `tool_input`, `tool_response`, `stop_hook_active`.

`transcript_path` is what makes this cheap: the hook is handed the exact file
`distill.build_tree()` already parses. We do not need to reconstruct anything
from the hook payload, or hold state between turns — the transcript on disk is
the state, exactly as it is today.

### Which event, and how often

| option | fires | verdict |
|---|---|---|
| `Stop` | end of every assistant turn | What "as the session grows" wants — but a 1145-turn session fires 1145 times. Needs debounce. |
| `SessionEnd` | once, when the session ends | Cheapest, but loses in-flight visibility and may not fire if the process is killed. |
| `PostToolUse` | every tool call | Far too chatty. No. |

**Proposed: `Stop` with a debounce, plus `SessionEnd` as a flush.** Publish only
when the last publish was more than N seconds ago (default 120) *or* the digest
gained new goals. Both events are cheap because distillation is incremental —
the same `(size, mtime_ns)` signature the index already uses tells us whether
anything changed.

### Hard constraint: a hook must never hurt the session

Hooks run in the session's path. Three rules, non-negotiable:

1. **Always exit 0.** A publish failure must never fail a developer's turn.
2. **Detach and cap.** Do the work in a background process with a hard timeout;
   the hook itself returns immediately.
3. **Never prompt, never write to stdout.** Log to a file under the store.

A dev whose session stutters because of a reporting tool will disable the
reporting tool.

---

## 2. What gets shipped: the raw transcript, unprocessed

The developer's machine does **no** processing. It copies bytes. All extraction
stays PM-side, exactly where it is today.

This is the right call, and for a better reason than simplicity:

- **The extraction logic keeps improving.** Every digest ships frozen at
  whatever `distill.py` looked like that week. Raw transcripts can be
  re-distilled forever — including by a version of the pipeline that does not
  exist yet. Three real distillation bugs were found and fixed in this repo's
  first week; digests published before those fixes would be permanently wrong.
- **The hook stays trivially fast and unbreakable.** No parsing, no model call,
  no dependency on the pipeline being installed dev-side. A file copy cannot
  throw a parse error into someone's session.
- **Zero cost and zero compute for the developer.** All model spend stays with
  the PM, which is also where the benefit lands.
- **The PM can debug.** When an entry looks wrong, the ground truth is there.

### The cost, measured on this machine

| | |
|---|---|
| sessions touched in 7 days | 68 |
| raw volume, one developer, one week | **336 MB** |
| largest single transcript | **197 MB** |
| one developer, per month | ~1.4 GB |
| ten developers, per month | ~14.5 GB |

At R2 pricing that is roughly **$0.22/month for a team of ten** — storage is
not the constraint.

**Bandwidth is**, if we are naive about it. A 197 MB transcript re-uploaded on
every `Stop` would be indefensible.

### Incremental append is what makes raw viable

Transcripts are append-only JSONL, which is precisely why abandoned branches
survive in them. So publish **byte ranges, not files**: keep the last published
offset per session and upload only what is new.

Measured on real sessions here, one turn is tiny:

| session | turns | p50 delta | p95 | max |
|---|---|---|---|---|
| `5eba26ea` (50 MB) | 2245 | **4.9 KB** | 30.7 KB | 3.3 MB |
| `76cd5aeb` (37 MB) | 1436 | **4.9 KB** | 47.1 KB | 2.1 MB |
| `082d912d` (34 MB) | 2307 | **3.4 KB** | 18.3 KB | 3.0 MB |

A turn ships about 5 KB. Those transcripts only reach tens of megabytes because
they run for days. The occasional multi-megabyte turn is one large tool result,
still far inside every limit.

So `Stop` firing 2,245 times is not a problem: that is 2,245 small writes over
days, roughly a cent of R2 operations. A short debounce (5s) is still worth
having to coalesce bursts, but the volume never needed a digest to be viable.

One guard: compaction can rewrite a transcript rather than extend it. Store the
size and a hash of the first N bytes alongside the offset; if the size shrinks
or the prefix hash changes, fall back to a full re-upload for that session.
(`PreCompact` and `PostCompact` hooks exist and could signal this explicitly —
worth testing which is more reliable.)

### The exposure, stated plainly

Raw transcripts contain whatever `Bash` printed (env vars, connection strings),
whatever `Read` returned (`.env` files, customer data), and every prompt typed.
Shipping them is a policy decision, not a technical one, and it is defensible
when the store is private and the team has agreed. What it requires:

- a **private** bucket — never a public one, never a repo that might go public
- encryption in transit and at rest
- a **warning, not a block**, at publish time when a secret-shaped string is
  detected (`sk-`, `ghp_`, `AKIA`, `-----BEGIN`, `postgres://`): the developer
  should know what they just shared, and be able to unpublish
- `ezcl unpublish <session>` — because the answer to "I just leaked something"
  cannot be "open a support ticket"

---

## 3. Transport

Volume changes the ranking versus digests: 14.5 GB/month rules out anything
git-shaped.

| option | auth | infra | verdict |
|---|---|---|---|
| **Shared folder** (Seafile / Tailscale) | already solved | none | Best first step. Publishing is a file write, pulling is a directory read. Proves the loop with zero new services, and you already run both. |
| **Object storage** (R2 / S3) | per-dev scoped token | bucket + tokens | The real answer. Range uploads are native, storage is pennies, egress on R2 is free. |
| **Git repo** | existing creds | one repo | **No.** Gigabytes of churning JSONL is the wrong shape for git. |
| **Postgres** (Neon) | connection string | database | Fine for the *index* of what has been published; wrong for the payload. |

**Proposed:** a `Transport` interface with a local-directory implementation
first, Cloudflare second. The pipeline should not know which is in use.

### 3a. The Cloudflare shape

Workers + R2 + D1 is sufficient, and Durable Objects are not needed. Verified
limits, because two of them dictate the design:

| limit | value | consequence |
|---|---|---|
| Worker request body | **100 MB** (Free/Pro) | A 197 MB transcript cannot be POSTed to a Worker |
| Worker isolate memory | 128 MB | It cannot be buffered either |
| Worker CPU | 10 ms free / 30 s paid | Fine — we stream, we do not compute |
| R2 object | 5 TiB, 10,000 parts | Not a constraint |
| R2 writes to one key | **1/sec**, then HTTP 429 | Never append to a single hot key |

R2 objects are immutable — there is no append. Both facts point the same way:

**Chunk objects, keyed by byte offset.**

```
r2://ezupdate/raw/<author>/<session>/<offset>-<length>.jsonl
```

- A turn's delta is kilobytes, so the **Worker can proxy it directly** through
  an R2 binding. No presigned URLs, no multipart, no client AWS signing.
- A 197 MB backfill is the *same protocol* — the client already thinks in byte
  ranges, so it just sends more chunks, each capped at ~8 MB. That stays far
  under every limit and needs no second code path.
- Each chunk is a distinct key, so the 1-write-per-second-per-key ceiling is
  never approached.
- The PM's pull concatenates chunks in offset order and gets a byte-identical
  transcript, which the existing pipeline reads unchanged.

**D1 holds the index, never the payload:** devices and their token hashes,
sessions with author/project/branch, chunk rows (offset, length, sha256),
enabled state, publish and unpublish markers. Rows are tiny and the queries are
trivial — `sessions since cursor`, `chunks for session`.

**Durable Objects are not required.** The only thing they would coordinate is a
sequence number, and the byte offset *is* the sequence — it comes from the
client, is globally ordered within a session, and makes writes idempotent
(re-sending the same range overwrites the same key with identical bytes). Skip
the extra moving part.

**Endpoints:**

```
POST /v1/chunk      device token · session, offset, length, sha256, body
POST /v1/session    upsert session metadata (project, branch, author, level)
DELETE /v1/session  unpublish — deletes chunks and tombstones the row
GET  /v1/sessions   PM: sessions changed since a cursor
GET  /v1/chunks     PM: chunk list for a session
GET  /v1/blob       PM: one chunk's bytes
```

**Cost.** At ten developers and ~14.5 GB/month, R2 storage is about $0.22/mo;
writes at roughly 4,000 chunks per developer per month land near $0.18/mo in
Class A operations, and R2 has no egress fee. Both sit inside or beside the
free tiers. **The bill is not the reason to hesitate; the retention policy is.**

---

## 4. The two sides

### Developer

```bash
ezcl hook install       # installs the hook, INERT. Shares nothing.
ezcl share on|off       # this session — the only thing that starts sharing
ezcl share status       # what is being shared right now, and why
ezcl publish --dry-run  # exactly which bytes would leave this machine
ezcl unpublish <session>
ezcl sync [7d|30d]      # backfill sessions from before opt-in
```

**Installed ≠ enabled. Default is off, and off means zero bytes leave.** The
hook is present and inert until someone opts in. Installing must never be the
thing that starts sharing — otherwise a teammate running a setup script has
opted in without knowing.

### Sharing is per session, with an optional per-repo default

Resolution, first match wins:

| # | source | meaning |
|---|---|---|
| 1 | `~/.ezchangelog/sessions/<session-id>.share` | This session, explicitly on or off. Set by `ezcl share`. Always wins. |
| 2 | `<repo>/.ez/config.json` → `"share"` | `always` = on unless this session opted out · `never` = hard off, `share on` refuses · `ask` = off, but announce how to turn it on |
| 3 | built-in default | **off** |

`.ez/` is a normal, committed directory in the repo — that is the point. A
policy that lives in someone's dotfiles is invisible to the team; a policy that
lives next to the code can be read, reviewed and changed in a pull request.

```jsonc
// <repo>/.ez/config.json
{
  "share": "ask",                    // always | ask | never
  "store": "https://ez.example.workers.dev",
  "exclude": ["**/secrets/**"]       // paths never published from this repo
}
```

### Turning it on mid-session

`CLAUDE_CODE_SESSION_ID` is exported into the environment of commands run
inside Claude Code — verified on this machine, where it returned the exact uuid
of the running session's transcript. So from inside a session:

```
! ezcl share on
```

resolves the current session with no guessing, no "most recent session"
heuristic, and no session-id argument to copy. A thin `/ezupdate` skill can
wrap it for people who prefer a slash command.

`hook install` writes `Stop` and `SessionEnd` entries into
`~/.claude/settings.json`. The hook reads the same three-level resolution above
on every fire, so `ezcl share off` takes effect on the very next turn — there
is no state cached in the running session to get stale.

### Visible in-session, not just in a config file

Both surfaces exist in the installed binary and both should be used:

**Persistent — `statusLine`.** A `statusLine` command in settings renders on
every turn. When sharing is on for this session it shows, always:

```
⬆ ezupdate · poslulu · sharing to team
```

It is on screen for the whole session. Nobody can be sharing without seeing it.

**Announced — `SessionStart` hook returning `systemMessage`.** Hook output
supports `systemMessage`, `additionalContext`, `hookSpecificOutput`,
`suppressOutput` and `continue`. A `SessionStart` hook can print once:

```
ezupdate is ON for this session — the full transcript is shared with the
team store. Run `ezcl hook disable` to stop. Nothing before now was shared.
```

When sharing is **off**, both stay silent. A tool that nags about being
disabled trains people to ignore it.

### Syncing a session from before opt-in

`~/.claude/projects` already holds the history, so backfill is a listing
problem, not a capture problem — and the picker for it already exists.

```bash
ezcl sync 30d       # opens the same checkbox picker, ticks nothing by default
```

Design rules that follow from consent:

- **Nothing is ticked by default.** Backfill is an explicit, per-session choice.
- **It reuses the existing picker**, so a developer sees exactly which sessions,
  which project, how many turns, before anything moves.
- A `published` marker in the local index records what has already gone, so the
  picker can show a `SYNCED` column and re-running is idempotent.
- Retroactive sharing is the sharpest edge in the whole design: a session from
  three weeks ago was conducted with no expectation of an audience. Never
  automatic, never a `--all` flag without confirmation.

### Project manager

```bash
ezcl pull                      # fetch new digests from the transport
./collect 7d                   # picker now includes remote sessions
```

Because raw transcripts arrive, **the entire existing pipeline runs unchanged**
— DISTILL, SEGMENT, BRIEF, COMPOSE, LINK, RENDER all work on a pulled
transcript exactly as they do on a local one. That is the payoff for shipping
raw. The changes are small:

- `ezcl pull` writes into the store beside local transcripts, tagged by author.
- The picker gains an **AUTHOR** column; local and remote sessions share a list.
- The **GIT stage** cannot inspect a repo the PM has not checked out. Either the
  PM has the repo (common — they usually do) and it works as-is, or commits
  travel alongside the transcript as a small sidecar file.
- The journal gains a grouping axis: **by person**, alongside project and date.

---

## 5. Phases

**Phase 1 — consent surface, sharing nothing.** `hook install/enable/disable/
status`, the `statusLine` indicator, the `SessionStart` announcement, and
`publish --dry-run`. Ships before anything can leave a machine. Done when a
developer can see exactly what would be shared and prove it is off.

**Phase 2 — publish loop, local transport.** Incremental byte-range upload with
the compaction guard, shared-folder transport, `unpublish`. Done when a second
machine's session appears in a PM's picker.

**Phase 3 — PM consumption.** `ezcl pull`, author column, per-person grouping.
Small, because the pipeline itself does not change.

**Phase 4 — backfill + real transport.** `ezcl sync` over the existing picker,
`SYNCED` column, object storage with per-dev tokens, retry and backlog.

**Phase 5 — policy.** Secret-shaped-string warnings, an audit command listing
everything this machine has ever published, retention on the store.

---

## 6. Risks

**Consent is the whole ballgame**, and shipping raw raises the stakes rather
than lowering them: what travels is not a summary of the work, it is everything
typed and everything printed. That is why the consent surface is Phase 1 and
the transport is Phase 2 — if the first version feels like monitoring, no one
runs `enable` a second time.

**Secrets.** Real, and the mitigation is warn-plus-unpublish, not a filter that
silently mangles a transcript the PM will later need to trust.

**Retention.** Raw transcripts accumulate at ~1.4 GB per developer per month
and never stop being sensitive. The store needs a retention policy from the
start — a year of ten developers' raw sessions is a liability nobody decided
to take on.

**Hook reliability.** `Stop` fires a lot; publishing must be debounced,
detached, capped, and silent on failure.

**Partial sessions.** A session publishes repeatedly as it grows. Digests must
be keyed by session id and replaced wholesale, not appended, or the PM will
journal the same work three times.

**Sessions that span machines.** A dev on two machines produces two digests for
one thread of work; the COMPOSE stage already merges across sessions, so this
is a labelling problem, not a new one.

**Cost stays PM-side.** Distillation is free, so a developer publishing costs
nothing beyond a `summary`-level `claude -p` call — worth measuring before
defaulting `summary` on.

---

## 7. Open decisions

1. **Does `"share": "always"` in a repo config need a confirmation the first
   time a developer works in that repo?** A committed file that silently
   enables sharing on someone's next clone is exactly the failure mode the
   whole consent design exists to prevent. Leaning yes: `always` means "on
   after this machine has acknowledged it once".
2. **Retention** — how long does the store keep raw transcripts before they are
   deleted or reduced to digests?
3. **Who can read the store?** Only the PM, or the whole team? "My manager can
   read my transcripts" and "my colleagues can" are very different agreements.
4. **Does `sync` backfill need the repo policy to allow it**, or is an explicit
   per-session tick always sufficient?
