# The ezup runner

A scheduled container that produces the team journal without the PM's laptop
being open. It is **the existing CLI, scheduled, on compute the PM owns** — not
a service, not a daemon, not a hosted feature. One run does exactly what a PM
types by hand today:

```
ezup pull            # decrypt every shared session the keyring can open
ezup collect ...     # run the pipeline over the pulled sessions
# then: upload the rendered journal to the PM's own bucket
```

See `docs/REMOTE-RUNNER-DESIGN.md` sections 3 and 4 for the design this
implements.

---

## The trust note (read this first)

The runner holds **the PM keyring and the LLM API key**, and it produces a
**plaintext** journal. Three facts follow, and they are not negotiable:

1. **It must run on compute the PM controls.** A Fly machine, a k8s CronJob, a
   home server, the PM's own GitHub Actions repo — anywhere the PM is the
   operator. Never a shared or multi-tenant box. Whoever operates the compute
   holds every reader key in the keyring and can decrypt every teammate's
   transcripts; that is precisely the end-to-end guarantee the store was built
   to keep, and a shared runner silently throws it away.

2. **The journal is the most sensitive artifact in the system** — a cross-team
   aggregate of everyone's work, decisions, and failures for the week,
   pre-digested for a reader. It is born in the runner's `/data` volume and
   uploaded **only to a bucket the PM owns**. It is *never* sent to the ezup
   store's R2: the store's promise is that it holds no plaintext, and the
   journal is not the exception.

3. **The LLM provider sees the distilled transcript content** in prompts.
   Choosing the provider *is* choosing who may read the team's work summaries.
   A PM who cannot accept a hosted model points `EZUP_LLM_BASE_URL` at a
   self-hosted, OpenAI-compatible endpoint.

---

## Container contract

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `EZUPDATE_STORE` | **yes** | The ezup store URL, e.g. `https://ezupdate.nyf.workers.dev`. `EZUP_STORE` is accepted as an alias. |
| `EZCHANGELOG_HOME` | no (default `/data`) | Store root and trust-domain volume: pull state, decrypted `pulled/`, and rendered `journals/`. |
| `EZUP_KEYRING` | one of these | The reader keys, **inline**: one `ezr_...` key per line, optional `ezr_... label`. Blank lines and `#` comments ignored. |
| `EZUP_KEYRING_FILE` | one of these | Path to a file holding the same key list (a Docker/K8s file secret). Preferred over `EZUP_KEYRING`. |
| `EZUP_LLM_PROVIDER` | no (default `http`) | The runner uses `http`; `claude-cli` is not installed in the image. |
| `EZUP_LLM_BASE_URL` | **yes (http)** | OpenAI chat-completions compatible base URL, e.g. `https://api.minimax.io/v1`. |
| `EZUP_LLM_API_KEY` | **yes (http)** | Provider bearer token. |
| `EZUP_LLM_API_KEY_FILE` | alt to above | Path to a file holding the bearer token; **preferred** over the env var (it stays out of `docker inspect`). |
| `EZUP_LLM_MODEL_MECHANICAL` | **yes (http)** | Model id for the cheap BRIEF/LINK tier (runs once per session). |
| `EZUP_LLM_MODEL_SYNTHESIS` | **yes (http)** | Model id for the COMPOSE tier (runs once per journal — the best model). |
| `EZUP_COLLECT_WINDOW` | no (default `all`) | The collect selection: `all` = every pulled session; a since window like `7d` also works. |
| `EZUP_COLLECT_SINCE` | no | If set, passed as `--since` to bound the time window explicitly. |
| `S3_BUCKET` | **yes** | The PM's OWN bucket for the plaintext journal. |
| `S3_ENDPOINT` | no (AWS S3) | S3-compatible endpoint for R2/MinIO/B2. Omit for real AWS S3. |
| `S3_REGION` | no | Region, when the endpoint requires one. |
| `S3_PREFIX` | no | Key prefix inside the bucket, e.g. `journals`. |
| `AWS_ACCESS_KEY_ID` | **yes** | Bucket credential (read by awscli). |
| `AWS_SECRET_ACCESS_KEY` | **yes** | Bucket credential (read by awscli). |

Secrets are read from `*_FILE` paths first, env second, and are **never echoed**
— only key fingerprints and counts reach the log.

### Mounted secrets (read-only, tmpfs where the platform allows)

```
/run/secrets/keyring.keys   the reader keys: one "ezr_..." per line (+ optional label)
/run/secrets/llm_api_key    the provider bearer token, bare string
```

Point `EZUP_KEYRING_FILE` and `EZUP_LLM_API_KEY_FILE` at these.

### Volume

```
/data   persistent, and IS the trust domain — encrypt it at rest with the
        platform's disk encryption. Holds pull state, pulled/<author>/*.jsonl
        (plaintext after decrypt), and journals/<id>/journal.html|md + entries.json.
```

### What the run produces and where it goes

The pipeline writes `journals/<id>/{journal.html, journal.md, entries.json}`.
The uploader sends all three to the PM's bucket under two keys each:

```
s3://$S3_BUCKET/$S3_PREFIX/<UTC-date>/journal.html   # immutable per-run record
s3://$S3_BUCKET/$S3_PREFIX/latest/journal.html       # stable, overwritten each run
```

(`journal.md` and `entries.json` alongside.) Content types are set so a browser
renders `journal.html` instead of downloading it.

### Exit code

The run exits **non-zero on any failure** — a missing key, an empty selection,
a failed model call, a failed upload. That is the entire monitoring story: a
failed run is a failed cron/CI job, which every scheduler already alerts on.
There is no health endpoint and no daemon.

---

## Build

Build from the **repository root** (the Dockerfile is under `runner/`):

```
docker build -f runner/Dockerfile -t ezup-runner .
```

The image is `python:3.12-slim` + `uv` + this package + `git` + AWS CLI v2. It
does **not** contain the `claude` CLI: the runner drives the HTTP provider, so
there is no `claude -p` and no interactive auth.

---

## Run it once

Create the two secret files locally (never commit them):

```
mkdir -p secrets data
printf 'ezr_alicekey alice\nezr_bobkey bob\n' > secrets/keyring.keys
printf 'sk-your-minimax-key' > secrets/llm_api_key
chmod 600 secrets/*
```

Then:

```
docker run --rm \
  -e EZUPDATE_STORE=https://ezupdate.nyf.workers.dev \
  -e EZUP_KEYRING_FILE=/run/secrets/keyring.keys \
  -e EZUP_LLM_PROVIDER=http \
  -e EZUP_LLM_BASE_URL=https://api.minimax.io/v1 \
  -e EZUP_LLM_API_KEY_FILE=/run/secrets/llm_api_key \
  -e EZUP_LLM_MODEL_MECHANICAL=MiniMax-Text-01 \
  -e EZUP_LLM_MODEL_SYNTHESIS=MiniMax-M1 \
  -e EZUP_COLLECT_WINDOW=all \
  -e S3_BUCKET=my-team-journals \
  -e S3_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com \
  -e S3_PREFIX=journals \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -v "$PWD/data:/data" \
  -v "$PWD/secrets:/run/secrets:ro" \
  ezup-runner
```

`docker compose -f runner/docker-compose.yml run --rm runner` does the same
with the values baked into `docker-compose.yml`.

---

## MiniMax provider settings

MiniMax speaks the OpenAI chat-completions shape the HTTP provider expects:

```
EZUP_LLM_PROVIDER          http
EZUP_LLM_BASE_URL          https://api.minimax.io/v1
EZUP_LLM_MODEL_MECHANICAL  MiniMax-Text-01      # cheap tier: BRIEF / LINK
EZUP_LLM_MODEL_SYNTHESIS   MiniMax-M1           # best tier: COMPOSE
```

The two-model split is load-bearing: BRIEF runs once per session on a cheap
model, COMPOSE runs once per journal on the best one. Collapsing them either
overspends or degrades the one stage that forms judgment. Model ids drift —
confirm the current names against MiniMax's model list before the first run.
Any OpenAI-compatible endpoint (OpenRouter, vLLM, Anthropic's compat endpoint,
a self-hosted model) works the same way; only `EZUP_LLM_BASE_URL` and the two
model ids change.

---

## Scheduling

The unit of work is a batch job with a weekly cadence. Pick whichever scheduler
runs on compute the PM controls. Suggested cadence: **weekly, Monday 06:00**.

### plain crontab (home server / VM the PM owns)

`crontab -e`, then:

```
# 06:00 every Monday. Uses file-mounted secrets under /srv/ezup.
0 6 * * 1 docker run --rm \
  --env-file /srv/ezup/runner.env \
  -v /srv/ezup/data:/data \
  -v /srv/ezup/secrets:/run/secrets:ro \
  ezup-runner >> /var/log/ezup-runner.log 2>&1
```

A non-zero exit lands in the log; wire the host's cron MAILTO or a log monitor
to alert on it.

### GitHub Actions

See `runner/github-actions.yml` — copy it to `.github/workflows/` in a repo the
PM controls, set the four Actions secrets it names, and it runs Monday 06:00 UTC
(plus a manual `workflow_dispatch` button). GitHub-hosted runners are ephemeral,
so `/data` does not persist between runs; `ezup pull` simply re-fetches its
window each time.

### Fly.io

Deploy the image and schedule a machine. In `fly.toml`:

```
[build]
  dockerfile = "runner/Dockerfile"

[[mounts]]
  source = "ezup_data"
  destination = "/data"

# A scheduled machine runs the entrypoint on the cron cadence, then stops.
[processes]
  runner = ""   # image ENTRYPOINT is the runner

[[services]]
  # none — this is a batch machine, not a served app
```

Set secrets with `fly secrets set EZUP_LLM_API_KEY=... AWS_SECRET_ACCESS_KEY=...`
and create the scheduled machine:

```
fly machine run . \
  --schedule weekly \
  --volume ezup_data:/data \
  --env EZUPDATE_STORE=https://ezupdate.nyf.workers.dev \
  --env EZUP_LLM_BASE_URL=https://api.minimax.io/v1 \
  --env EZUP_LLM_MODEL_MECHANICAL=MiniMax-Text-01 \
  --env EZUP_LLM_MODEL_SYNTHESIS=MiniMax-M1 \
  --env EZUP_COLLECT_WINDOW=all \
  --env S3_BUCKET=my-team-journals \
  --env S3_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com \
  --env S3_PREFIX=journals
```

(Mount the keyring and LLM key as Fly secrets or a secret volume; `fly machine
run --schedule` accepts `hourly|daily|weekly|monthly`.)

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ezup-runner
spec:
  schedule: "0 6 * * 1"     # Monday 06:00
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0        # a failed run is a failed job; surface it
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: runner
              image: ezup-runner
              env:
                - { name: EZUPDATE_STORE, value: https://ezupdate.nyf.workers.dev }
                - { name: EZUP_KEYRING_FILE, value: /run/secrets/ezup/keyring.keys }
                - { name: EZUP_LLM_BASE_URL, value: https://api.minimax.io/v1 }
                - { name: EZUP_LLM_API_KEY_FILE, value: /run/secrets/ezup/llm_api_key }
                - { name: EZUP_LLM_MODEL_MECHANICAL, value: MiniMax-Text-01 }
                - { name: EZUP_LLM_MODEL_SYNTHESIS, value: MiniMax-M1 }
                - { name: EZUP_COLLECT_WINDOW, value: all }
                - { name: S3_BUCKET, value: my-team-journals }
                - { name: S3_ENDPOINT, value: "https://<accountid>.r2.cloudflarestorage.com" }
                - { name: S3_PREFIX, value: journals }
                - { name: AWS_ACCESS_KEY_ID,     valueFrom: { secretKeyRef: { name: ezup-bucket, key: access_key_id } } }
                - { name: AWS_SECRET_ACCESS_KEY, valueFrom: { secretKeyRef: { name: ezup-bucket, key: secret_access_key } } }
              volumeMounts:
                - { name: data,    mountPath: /data }
                - { name: secrets, mountPath: /run/secrets/ezup, readOnly: true }
          volumes:
            - name: data
              persistentVolumeClaim: { claimName: ezup-data }   # encrypted-at-rest PVC
            - name: secrets
              secret: { secretName: ezup-keyring }              # keyring.keys + llm_api_key
```

---

## Viewing the result

The journal lands in `/data/journals/<id>/journal.html` (self-contained) and in
the PM's bucket. Simplest paths, in order of effort:

1. Read the object from the bucket — the `latest/journal.html` key is a stable
   bookmark, and if the bucket is served over HTTP it renders in the browser
   (the uploader sets `Content-Type: text/html`).
2. Reach the `/data` volume directly (scp, a tailnet file share, `python -m
   http.server` bound to localhost).

Sharing with the team is a per-journal PM decision (forward the HTML), never a
default: the journal names who did what, and the PM is the editor of record
before it circulates.
