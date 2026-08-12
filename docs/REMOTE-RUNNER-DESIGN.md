# Remote Pipeline Runner — Design

Status: design only. Nothing in this document is built yet; it names the files
and seams the next workflow will implement.

The goal: the PM should not need a laptop open to get a journal. Something in
the PM's own infrastructure pulls the team's encrypted transcripts, decrypts
them, runs the existing pipeline (`ezchangelog/pipeline.py`), and leaves a
rendered journal where the PM can read it.

The one constraint everything else follows from: **the store is end-to-end
encrypted, so the runner is not a feature of the store — it is a feature of
the PM's trust domain.** It only works because the PM hands it the keyring.


## 1. Trust boundary

Where plaintext exists, end to end:

```
  DEV MACHINE                 STORE (Cloudflare)            PM's RUNNER            LLM API
  ~/.claude/**.jsonl          Worker + R2 + D1              PM's own container     MiniMax / Anthropic / ...
 ┌─────────────────────┐     ┌──────────────────────┐     ┌───────────────────┐  ┌──────────────────┐
 │ PLAINTEXT            │    │ CIPHERTEXT ONLY       │    │ PLAINTEXT          │ │ PLAINTEXT        │
 │ transcript on disk;  │───▶│ AES-256-GCM chunk     │───▶│ keyring decrypts;  │▶│ prompts contain  │
 │ encrypted client-side│    │ bodies; server holds  │    │ pipeline runs on   │ │ distilled        │
 │ before put_chunk()   │    │ only sha256(token),   │    │ reassembled        │ │ transcript       │
 │                      │    │ metadata, key hashes  │    │ transcripts        │ │ content          │
 └─────────────────────┘     └──────────────────────┘     └───────────────────┘  └──────────────────┘
        holds ezu_ key            holds NO key              holds ezr_ keyring      holds no key;
                                                            + LLM api key           sees content
```

Four zones, two of which ever see plaintext *by design*:

- **Dev machine.** Plaintext is native here; it is where the transcript is
  written. The `ezu_` key encrypts before upload and never leaves.
- **Store.** Ciphertext only. The worker authenticates on a value derived from
  the pasted key and stores hashes; even a fully malicious worker cannot
  decrypt a chunk. This is the property the whole encryption phase bought.
- **PM's runner.** Plaintext again — necessarily, because the runner holds the
  PM's keyring. This is not a leak; it is the PM's own trust domain, the same
  domain as the PM's laptop running `ezup pull` today. The runner is "the PM's
  laptop, headless."
- **LLM API.** The provider sees the distilled transcript content in prompts.
  This is inherent to using a hosted model and is a deliberate PM decision:
  choosing the provider *is* choosing who may read the team's work summaries.
  A PM who cannot accept it points the HTTP provider at a self-hosted endpoint.

### What breaks if the runner is shared or third-party

Do not build a "hosted runner" as a convenience feature. If the runner runs on
compute the PM does not control — a shared service, a runner baked into the
worker, a multi-tenant box — then:

1. **E2E is gone, silently.** The operator of that compute holds every reader
   key in the keyring and can decrypt every transcript of every team using it.
   The store's "server cannot read your data" guarantee becomes marketing.
2. **The keyring becomes a server-side secret**, which is exactly the class of
   secret this system was redesigned to eliminate. One compromise of the shared
   runner is a compromise of all tenants' history, retroactively.
3. **The consent model breaks.** Developers consented to sharing with *their
   PM* (the holder of the reader key), not with an operator. A third-party
   runner adds a reader nobody opted in to.

The design therefore has no runner registration, no runner API on the worker,
and no code path where the worker ever sees a decryption key. The runner is
just the existing CLI, scheduled, in a container the PM owns.

### Legacy plaintext

The encryption cutover re-mints all tokens and **re-uploads the existing ~10 MB
encrypted; plaintext chunks are deleted**, rather than marking them
legacy-plaintext. Rationale: developers still hold every transcript locally, so
`ezup sync all` regenerates the store losslessly, and the runner (and viewer,
and pull) then need exactly one format — ciphertext. A legacy-plaintext flag
would be a permanent second code path in four places to save one afternoon of
re-upload.


## 2. LLM provider abstraction

Today `ezchangelog/llm.py` is one function, `run()`, that shells out to
`claude -p` and streams `stream-json` events. The pipeline's contract with it
is small and must survive unchanged:

- `llm.run(prompt, *, model, effort, system, on_text, cwd) -> Reply`
- `llm.MECHANICAL` / `llm.SYNTHESIS` — the two tiers
- `Reply(text, cost_usd, input_tokens, output_tokens, duration_ms)`
- streaming deltas via `on_text` (the console renders them live)
- `llm.extract_json()` (pure text, provider-independent)
- `llm.available()` (preflight check)
- `LLMError` on any failure

### Files and seams

- **`ezchangelog/llm.py`** stays the facade. `run()` keeps its exact signature
  and delegates to a resolved provider; `MECHANICAL`, `SYNTHESIS`, `Reply`,
  `LLMError`, `extract_json` stay here. `pipeline.py` does not change at all —
  that is the point of the seam.
- **`ezchangelog/llm_providers.py`** (new) holds:
  - `class Provider(Protocol)` with `run(prompt, *, model, effort, system,
    on_text, cwd) -> Reply` and `available() -> bool`. Same shape as today's
    module-level function, deliberately: the facade is a one-line dispatch.
  - `ClaudeCliProvider` — the current subprocess code, moved verbatim. Default
    when no provider is configured, so nothing changes for existing users.
  - `HttpProvider(base_url, api_key, model_map, timeout)` — OpenAI
    chat-completions compatible (`POST {base_url}/chat/completions`,
    `Authorization: Bearer <api_key>`, `stream: true`, SSE
    `data: {...}` lines, `delta.content` pieces fed to `on_text`). MiniMax,
    OpenRouter, vLLM, and Anthropic's compatibility endpoint all speak this.
    Implemented on `urllib.request` with incremental reads — the client's one
    permitted dependency is `cryptography`, so no `requests`/`httpx`.
  - `resolve_provider(env) -> Provider` — reads configuration once.

### Tier mapping, not model names

The pipeline asks for `("sonnet", "low")` and `("opus", "max")`. Those names
are Claude-CLI vocabulary, so the HTTP provider must not receive them raw.
The seam: `HttpProvider` carries a `model_map` translating the tier's alias to
a concrete model id, and maps `effort` to the OpenAI-compatible
`reasoning_effort` when the endpoint accepts it (dropped otherwise — it is an
optimization hint, not a correctness input).

Configuration (environment, or the runner config file in section 3):

```
EZUP_LLM_PROVIDER          claude-cli (default) | http
EZUP_LLM_BASE_URL          e.g. https://api.minimax.io/v1
EZUP_LLM_API_KEY           bearer key; never written to any config in a repo
EZUP_LLM_MODEL_MECHANICAL  model id used for the BRIEF / LINK tier
EZUP_LLM_MODEL_SYNTHESIS   model id used for the COMPOSE tier
```

Two model variables rather than one because the pipeline's economics depend on
the split: BRIEF runs once per session on a cheap model, COMPOSE once per
journal on the best one. Collapsing them would either overspend or degrade the
one stage that forms judgment.

`Reply` accounting: providers that report usage fill the token fields;
`cost_usd` stays `0.0` when the endpoint does not price responses (the console
already renders a zero cost gracefully). `available()` for the HTTP provider
is "base_url and api_key are set" — no network preflight, because the first
real call is the honest preflight and a HEAD adds a failure mode.


## 3. The runner

**Recommendation: a scheduled container, not a service.** No `ezup serve`, no
daemon, no queue. The unit of work is "pull, then run the pipeline once" —
a batch job with a natural cadence (nightly or weekly). A long-running process
would hold the decrypted keyring in memory around the clock to save nothing.

The job is, morally, exactly what the PM types by hand today:

```
ezup pull && ./collect 7d --include-pulled <headless-selection>
```

One small CLI addition is required: `collect` currently selects sessions
interactively (`--interactive`) or not at all. The runner needs
`--all-pulled` (or `--yes`): non-interactive, select every pulled session in
the window, proceed to the model stages. That flag is part of the build plan,
not a new mode — it reuses the existing chooser-less path in
`cmd_collect`.

### Container contract

Image: `python:3.12-slim` + `uv` + this repo (installed via `setup.sh`), plus
`git` (the GIT corroboration stage degrades gracefully without repos present,
but git itself must exist). No Claude CLI in the image unless the PM chooses
the `claude-cli` provider.

```
ENVIRONMENT
  EZUPDATE_STORE           https://ezupdate.nyf.workers.dev
  EZCHANGELOG_HOME         /data            store root (state, pulled/, journals/)
  EZUP_KEYRING             /run/secrets/keyring.json
  EZUP_LLM_PROVIDER        http
  EZUP_LLM_BASE_URL        provider endpoint
  EZUP_LLM_API_KEY_FILE    /run/secrets/llm_api_key   (preferred over env)
  EZUP_LLM_MODEL_MECHANICAL / EZUP_LLM_MODEL_SYNTHESIS

MOUNTED SECRETS (read-only, tmpfs where the platform allows)
  /run/secrets/keyring.json   the PM keyring: [{key: "ezr_...", label: "alice"}, ...]
                              Each entry authenticates AND decrypts its own scope;
                              pull iterates the ring and merges client-side.
  /run/secrets/llm_api_key    the provider bearer key, bare string

VOLUMES
  /data                    persistent. pull-state.json, pulled/<author>/*.jsonl
                           (plaintext after decrypt — this volume IS the trust
                           domain; encrypt it at rest with platform disk
                           encryption), journals/journal-*/journal.html|md

SCHEDULE
  cron on the PM's platform (a Fly machine on a schedule, a k8s CronJob,
  a plain crontab on a home server). Suggested: weekly, Monday 06:00.

ENTRYPOINT (sketch, ships as runner/entrypoint.sh)
  ezup pull                      # keyring loop: one pull per reader key
  ./collect 7d --include-pulled --all-pulled
```

Exit code is the job status; the platform's job history is the monitoring.
No custom health endpoint — a failed run is a failed cron job, which every
scheduler already alerts on.

### How the PM views the result

The journal lands in `/data/journals/<id>/journal.html`, self-contained. The
simplest viewing paths, in order of effort:

1. The volume is on a machine the PM can reach: open the file (scp, Tailscale
   file share, a `python -m http.server` bound to localhost/tailnet).
2. The runner mails or messages the file: journal.html is one attachment-sized
   artifact, and "email me the journal every Monday" is the actual product.
   A `--notify <cmd>` hook on the entrypoint keeps this out of core code.
3. (Later, optional) encrypted upload back to the store — see section 4.

Do not build a web UI for this in the next phase. The HTML is already the UI.


## 4. Where the journal goes

**The journal is sensitive — treat it as the most sensitive artifact in the
system.** It is a cross-team aggregate: one document summarizing everyone's
work, decisions, and failures for a week. A leaked transcript exposes one
person's session; a leaked journal exposes the whole team, pre-digested for a
reader. It also carries code snippets and commit references the COMPOSE stage
chose to surface.

Rules:

- The journal is **born and stays inside the PM's trust domain**: the runner's
  `/data` volume. It is never uploaded anywhere in plaintext, and in
  particular never `PUT` to the worker unencrypted — the store's promise is
  that it holds no plaintext, and the journal must not be the exception.
- Access control is therefore the platform's, not ours: who can read the
  volume, who receives the notification. This is deliberate — inventing an
  ezup-level ACL for a file on the PM's own disk adds surface without adding a
  boundary.
- If the PM wants journals in the store (history, the viewer), they go up
  **encrypted under a PM-held key** (the natural shape: the runner publishes a
  synthetic session, author `journal`, encrypted so that only reader keys the
  PM designates can open it). The viewer already merges keyrings, so a
  journal-bearing key slots in with zero viewer changes. This is a later
  phase; it must not block the runner.
- Sharing with the team is a PM decision made per journal (forward the HTML),
  not a default. The journal names who did what; the PM is the editor of
  record before it circulates.


## 5. Phased build plan (next workflow)

Each phase lands green (`.venv/bin/python -m unittest discover -s tests -t .`)
and is independently useful.

**Phase 1 — provider seam (no behavior change).**
Move the subprocess code from `llm.py` into `llm_providers.ClaudeCliProvider`;
`llm.run()` becomes a dispatch through `resolve_provider()`, defaulting to the
CLI provider. Tests: existing suite stays green; new tests pin the facade
contract (signature, `Reply` fields, `LLMError` on failure) against a fake
provider.

**Phase 2 — HTTP provider.**
`HttpProvider` on `urllib`: OpenAI-chat SSE streaming, `on_text` deltas, usage
accounting, tier `model_map`, `reasoning_effort` passthrough. Tests: a local
`http.server` stub emitting canned SSE; malformed-stream and non-200 paths
raise `LLMError` with the response body's first 400 chars (mirror the CLI
provider's error hygiene).

**Phase 3 — headless collect.**
`--all-pulled` on `ezcl collect`: non-interactive selection of every pulled
session in the window; refuse (exit non-zero, loud) when the window selects
nothing, so a broken pull cannot silently produce an empty journal. Tests:
selection logic over a fixture pull-state.

**Phase 4 — runner packaging.**
`runner/Dockerfile`, `runner/entrypoint.sh`, `runner/README.md` documenting the
container contract from section 3 verbatim (env, secrets, volume, schedule).
Secrets are read from `*_FILE` paths first, env second; the entrypoint never
echoes them. No deploy in this phase — the PM builds and schedules it on their
own platform.

**Phase 5 (optional, separate decision) — encrypted journal publish.**
`ezup publish-journal`: encrypt journal.html under a PM key and store it as a
synthetic session so the viewer can render journal history. Explicitly out of
scope until the PM asks for it.

Out of scope permanently: any runner that executes outside the PM's trust
domain, and any worker endpoint that receives a key or a plaintext byte.
