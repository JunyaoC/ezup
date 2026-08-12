#!/usr/bin/env bash
#
# The scheduled runner, once per invocation (REMOTE-RUNNER-DESIGN section 3).
#
# This is "the PM's laptop, headless": it holds the PM's keyring and the LLM
# key, so it runs only on compute the PM controls. One run does, in order:
#
#   1. materialise <store>/keyring.json from the mounted EZUP_KEYRING secret
#   2. `ezup pull`      -- decrypt every shared session the keyring can open
#   3. `ezup collect`   -- run the pipeline over the pulled sessions, HTTP LLM
#   4. upload journal.html + journal.md + entries.json to the PM's OWN bucket
#
# Every step is fatal: `set -e` plus explicit checks mean a failure exits
# non-zero, which is the whole monitoring story -- a failed run is a failed
# cron/CI job, and every scheduler already alerts on that. Secrets are never
# echoed; only fingerprints and counts reach the log.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -- logging -----------------------------------------------------------------
# Everything the runner says goes to stderr so stdout stays clean for any
# machine-readable manifest a caller might want to capture.
log()  { printf '\033[36m▸\033[0m %s\n' "$*" >&2; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✖\033[0m %s\n' "$*" >&2; exit 1; }

# -- 0. environment ----------------------------------------------------------
# EZCHANGELOG_HOME is the store root and the trust-domain volume: pull-state,
# decrypted pulled/ transcripts, and rendered journals all live under it. The
# CLI reads it directly; we default it so a bare `docker run` still works.
export EZCHANGELOG_HOME="${EZCHANGELOG_HOME:-/data}"
STORE="$EZCHANGELOG_HOME"
KEYRING_FILE="$STORE/keyring.json"

# The store URL. The design's canonical name is EZUPDATE_STORE (what the CLI
# reads); accept EZUP_STORE as a convenience alias so either spelling works.
if [ -z "${EZUPDATE_STORE:-}" ] && [ -n "${EZUP_STORE:-}" ]; then
  export EZUPDATE_STORE="$EZUP_STORE"
fi
[ -n "${EZUPDATE_STORE:-}" ] || die "EZUPDATE_STORE (or EZUP_STORE) is not set"

# The runner never uses `claude -p`; it drives the HTTP LLM provider. Default
# the provider so the PM only has to supply the endpoint, key, and model ids.
export EZUP_LLM_PROVIDER="${EZUP_LLM_PROVIDER:-http}"

# The *_FILE form is read directly by the provider (llm_providers._read_key),
# so we only validate it here. We deliberately do NOT cat it into an exported
# env var: that would copy the bare key from its tmpfs file into
# /proc/<pid>/environ of every child -- the exact `docker inspect` exposure the
# *_FILE form exists to avoid (review finding 5).
if [ -n "${EZUP_LLM_API_KEY_FILE:-}" ]; then
  [ -r "$EZUP_LLM_API_KEY_FILE" ] || die "EZUP_LLM_API_KEY_FILE is not readable: $EZUP_LLM_API_KEY_FILE"
fi

if [ "$EZUP_LLM_PROVIDER" = "http" ]; then
  [ -n "${EZUP_LLM_BASE_URL:-}" ]         || die "EZUP_LLM_BASE_URL is not set (http provider)"
  [ -n "${EZUP_LLM_API_KEY:-}" ]          || die "EZUP_LLM_API_KEY / EZUP_LLM_API_KEY_FILE is not set (http provider)"
  [ -n "${EZUP_LLM_MODEL_MECHANICAL:-}" ] || die "EZUP_LLM_MODEL_MECHANICAL is not set (http provider)"
  [ -n "${EZUP_LLM_MODEL_SYNTHESIS:-}" ]  || die "EZUP_LLM_MODEL_SYNTHESIS is not set (http provider)"
fi

mkdir -p "$STORE"

# -- 1. keyring --------------------------------------------------------------
# The task's contract: EZUP_KEYRING is the secret MATERIAL (several ezr_ reader
# keys), and this script writes it into <store>/keyring.json. That collides
# with the CLI's use of $EZUP_KEYRING as a path OVERRIDE (keyring.py), so we
# read the material, then unset the var: the CLI then resolves the keyring at
# its default <store>/keyring.json, which is exactly what we populate.
#
# Accepted forms, in precedence order:
#   EZUP_KEYRING_FILE  a path to a file holding the keys (K8s/Docker file secret)
#   EZUP_KEYRING       inline: one reader key per line, optional "<ezr_key> label"
# Lines that are blank or start with '#' are ignored. A key already in the ring
# is a hard duplicate error on a persistent volume, so we rebuild the ring from
# scratch every run -- that keeps re-runs idempotent.
keyring_material=""
if [ -n "${EZUP_KEYRING_FILE:-}" ]; then
  [ -r "$EZUP_KEYRING_FILE" ] || die "EZUP_KEYRING_FILE is not readable: $EZUP_KEYRING_FILE"
  keyring_material="$(cat "$EZUP_KEYRING_FILE")"
elif [ -n "${EZUP_KEYRING:-}" ]; then
  keyring_material="$EZUP_KEYRING"
fi

# The reader keys are the crown jewels: whoever holds them decrypts every
# FUTURE pull, not just what is already on disk. So the keyring must NEVER land
# on the persistent /data volume (a snapshot would leak every key). Build it on
# an ephemeral runtime path instead, point the CLI there via EZUP_KEYRING (which
# keyring_path() reads as a path override), and shred it when the run ends.
# (review finding 2).
KEYRING_FILE="$(mktemp "${TMPDIR:-/tmp}/ezup-keyring.XXXXXX")"
chmod 600 "$KEYRING_FILE"
export EZUP_KEYRING="$KEYRING_FILE"
# shellcheck disable=SC2064
trap "rm -f -- '$KEYRING_FILE'" EXIT INT TERM

if [ -n "$keyring_material" ]; then
  log "keyring: building an ephemeral ring from the mounted secret"
  # Rebuild from scratch: `ezup keyring add` refuses a duplicate keyid.
  rm -f "$KEYRING_FILE"
  added=0
  while IFS= read -r line || [ -n "$line" ]; do
    # Trim leading/trailing whitespace without spawning a subshell per line.
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    # "<token> <optional label...>": first field is the key, remainder a label.
    token="${line%%[[:space:]]*}"
    label=""
    if [ "$token" != "$line" ]; then
      label="${line#"$token"}"
      label="${label#"${label%%[![:space:]]*}"}"
    fi
    case "$token" in
      ezr_*) : ;;
      *) die "keyring entry is not a reader (ezr_) key; refusing to add it" ;;
    esac
    if [ -n "$label" ]; then
      ezup keyring add "$token" --label "$label" >/dev/null
    else
      ezup keyring add "$token" >/dev/null
    fi
    added=$((added + 1))
  done <<EOF
$keyring_material
EOF
  [ "$added" -gt 0 ] || die "EZUP_KEYRING held no reader keys"
  ok "keyring: $added reader key(s) loaded"
elif [ -f "$KEYRING_FILE" ]; then
  # No material supplied but a keyring is already mounted at the default path.
  log "keyring: using existing $KEYRING_FILE"
else
  die "no keyring: set EZUP_KEYRING (inline keys) or EZUP_KEYRING_FILE, or mount $KEYRING_FILE"
fi

# -- 2. pull -----------------------------------------------------------------
# The keyring loop: one authenticated pull per reader key, decrypting each
# teammate's shared sessions into <store>/pulled/<author>/*.jsonl.
log "pull: fetching shared sessions for every keyring reader"
ezup pull
ok "pull: complete"

# -- 3. collect + journal ----------------------------------------------------
# Non-interactive selection of every pulled session in the window, then the
# BRIEF/COMPOSE/LINK pipeline over the HTTP provider. --json keeps stdout
# machine-readable; --quiet drops the live model stream from the logs.
#
# EZUP_COLLECT_WINDOW is the positional selection ("all" = every pulled session;
# the design also supports a since window like "7d"). EZUP_COLLECT_SINCE, when
# set, bounds the time window explicitly.
# The pipeline scope: "all" (every pulled project) is the positional sentinel;
# a time window like 7d/30d/2w is the --since flag, NOT a positional (a bare
# "30d" would be read as a directory named 30d and match nothing). (finding 1)
window="${EZUP_COLLECT_WINDOW:-all}"
collect_args=(all --include-pulled --yes --quiet --json)
since="${EZUP_COLLECT_SINCE:-}"
if [ "$window" != "all" ]; then
  since="$window"          # EZUP_COLLECT_WINDOW=30d means --since 30d
fi
if [ -n "$since" ]; then
  collect_args+=(--since "$since")
fi
log "collect: ezup collect ${collect_args[*]}"
# Capture the JSON so we read the exact journal path the CLI reports, rather
# than re-guessing it by directory mtime (finding 6).
collect_out="$(ezup collect "${collect_args[@]}")"
ok "collect: pipeline complete"

# -- 4. locate the journal ---------------------------------------------------
# The CLI printed {"journal": "<absolute path>", ...}; that is authoritative.
# Parsing it (rather than mtime-guessing a subdirectory) means we upload exactly
# the journal this run produced, even on a /data volume full of older runs.
journal_html="$(printf '%s' "$collect_out" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d.get("journal") or "")' 2>/dev/null)"
[ -n "$journal_html" ] || die "collect did not report a journal path (did it select anything?)"
journal_dir="$(dirname "$journal_html")"

for name in journal.html journal.md entries.json; do
  [ -f "$journal_dir/$name" ] || die "expected $journal_dir/$name was not produced"
done
ok "journal: $journal_dir"

# -- 5. upload to the PM's OWN bucket ----------------------------------------
# The rendered journal is PLAINTEXT synthesis of decrypted work, so it goes to
# the PM's own S3-compatible bucket (R2/S3/MinIO) -- NEVER the ezup store's R2.
log "upload: sending journal to the PM's bucket"
"$HERE/upload.sh" "$journal_dir"
ok "upload: complete"

ok "run finished"
