#!/usr/bin/env bash
#
# Upload one rendered journal to the PM's OWN S3-compatible bucket.
#
#   upload.sh <journal_dir>
#
# TRUST NOTE (REMOTE-RUNNER-DESIGN section 4): the journal is the most
# sensitive artifact in the system -- a cross-team, pre-digested aggregate of
# everyone's work. It is PLAINTEXT, and it goes ONLY to a bucket the PM
# controls. It must never be PUT to the ezup store's R2: the store's promise is
# that it holds no plaintext, and the journal is not the exception.
#
# The bucket is any S3 API (Cloudflare R2, AWS S3, MinIO, Backblaze B2 S3).
# Credentials come from the environment; awscli reads AWS_ACCESS_KEY_ID and
# AWS_SECRET_ACCESS_KEY itself, and S3_ENDPOINT points it at a non-AWS host.
set -euo pipefail

journal_dir="${1:-}"
[ -n "$journal_dir" ] || { echo "usage: upload.sh <journal_dir>" >&2; exit 2; }
[ -d "$journal_dir" ] || { echo "upload: not a directory: $journal_dir" >&2; exit 2; }

# -- required destination ----------------------------------------------------
[ -n "${S3_BUCKET:-}" ]           || { echo "upload: S3_BUCKET is not set" >&2; exit 2; }
[ -n "${AWS_ACCESS_KEY_ID:-}" ]   || { echo "upload: AWS_ACCESS_KEY_ID is not set" >&2; exit 2; }
[ -n "${AWS_SECRET_ACCESS_KEY:-}" ] || { echo "upload: AWS_SECRET_ACCESS_KEY is not set" >&2; exit 2; }

# S3_PREFIX is optional; normalise it to "" or "prefix/" (no leading slash, one
# trailing slash) so key concatenation is unambiguous.
prefix="${S3_PREFIX:-}"
prefix="${prefix#/}"
if [ -n "$prefix" ]; then
  prefix="${prefix%/}/"
fi

# A dated key so history accumulates rather than overwrites; the run's UTC date
# is stable within a single invocation.
run_date="$(date -u +%Y-%m-%d)"

# awscli talks to a non-AWS S3 endpoint via --endpoint-url. Build the common
# argument list once; omit --endpoint-url entirely for real AWS S3 (empty
# S3_ENDPOINT), where passing it would be wrong.
aws_common=()
if [ -n "${S3_ENDPOINT:-}" ]; then
  aws_common+=(--endpoint-url "$S3_ENDPOINT")
fi
if [ -n "${S3_REGION:-}" ]; then
  aws_common+=(--region "$S3_REGION")
fi

# Explicit content types so a PM who serves the bucket over HTTP gets a journal
# that renders in the browser instead of downloading as a blob.
content_type_for() {
  case "$1" in
    *.html) printf 'text/html; charset=utf-8' ;;
    *.md)   printf 'text/markdown; charset=utf-8' ;;
    *.json) printf 'application/json' ;;
    *)      printf 'application/octet-stream' ;;
  esac
}

put() {
  # put <local-file> <dest-key>
  local src="$1" key="$2" ctype
  ctype="$(content_type_for "$src")"
  aws s3 cp "$src" "s3://$S3_BUCKET/$key" \
    "${aws_common[@]}" \
    --content-type "$ctype" \
    --only-show-errors
  echo "  uploaded s3://$S3_BUCKET/$key" >&2
}

# The three artifacts the runner is contracted to publish.
for name in journal.html journal.md entries.json; do
  src="$journal_dir/$name"
  [ -f "$src" ] || { echo "upload: missing $src" >&2; exit 1; }
  # Dated copy: the immutable record for this run.
  put "$src" "${prefix}${run_date}/${name}"
  # "latest" copy: a stable key the PM can bookmark; overwritten each run.
  put "$src" "${prefix}latest/${name}"
done
