#!/usr/bin/env bash
# Wipe this machine's ezup state and start clean.
#
#   ./reset.sh          # ask before deleting
#   ./reset.sh --yes    # no prompt
#
# Removes the local store (~/.ezchangelog by default, or $EZCHANGELOG_HOME):
# device config, keyring, publish offsets, pulled transcripts, journals, logs.
# Does NOT touch the remote store, ~/.claude, or anything you published --
# use `ezup unpublish` / `ezup token revoke` for the server side.
set -euo pipefail

HOME_DIR="${EZCHANGELOG_HOME:-$HOME/.ezchangelog}"

# Keep the admin token if this machine happens to own the store -- it is not
# per-session state and re-creating it means rotating the worker secret.
ADMIN_BACKUP=""
if [ -f "$HOME_DIR/admin-token" ]; then
  ADMIN_BACKUP="$(cat "$HOME_DIR/admin-token")"
fi

if [ "${1:-}" != "--yes" ] && [ "${1:-}" != "-y" ]; then
  printf 'This deletes %s (device config, keyring, pulled sessions, journals).\n' "$HOME_DIR"
  printf 'The remote store is untouched. Continue? [y/N] '
  read -r answer
  case "$answer" in [yY]*) ;; *) echo "aborted"; exit 0 ;; esac
fi

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"
chmod 700 "$HOME_DIR"
if [ -n "$ADMIN_BACKUP" ]; then
  printf '%s' "$ADMIN_BACKUP" > "$HOME_DIR/admin-token"
  chmod 600 "$HOME_DIR/admin-token"
  echo "kept admin-token (store ownership)"
fi

cat <<'DONE'
clean. start again with:

  ezup device enroll --name YOU     # get a device (self-serve)
  ezup hook install                 # then /ezup on to share

or, for a PM given reader keys:

  ezup keyring add ezr_...
  ezup pull                         # fetch + journal the last 7 days
DONE
