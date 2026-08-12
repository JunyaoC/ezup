#!/usr/bin/env bash
# ezup hook shim. Finds the CLI and hands it the hook payload from stdin.
#
# Two invariants, in order of importance:
#   1. Exit 0 no matter what. A missing ezcl, a broken store, a dead network —
#      none of it may ever fail a developer's turn.
#   2. Without ezcl installed the plugin is INERT: nothing is read, nothing is
#      sent, nothing is printed. The plugin grants no consent by existing.
EZCL="$(command -v ezup 2>/dev/null || command -v ezcl 2>/dev/null)"
for CAND in "$HOME/.local/bin/ezup" "$HOME/.local/bin/ezcl"; do
  [ -z "$EZCL" ] && [ -x "$CAND" ] && EZCL="$CAND"
done
[ -z "$EZCL" ] && exit 0

# hook-run inherits hook_entry's own never-raise guarantee; the redirect is a
# belt over those braces for anything the OS itself throws.
"$EZCL" hook-run 2>/dev/null
exit 0
