#!/usr/bin/env bash
# One-shot setup for a fresh machine.
#
#   ./setup.sh
#
# Installs uv if missing, creates .venv, installs this package into it, and
# checks that the Claude CLI the pipeline shells out to is present.
# Safe to re-run: every step is idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON_VERSION="3.11"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
step() { printf '\n\033[36m▸\033[0m \033[1m%s\033[0m\n' "$1"; }

bold "ez-changelog setup"

# -- 1. uv -------------------------------------------------------------------
step "uv"
if command -v uv >/dev/null 2>&1; then
  ok "already installed ($(uv --version))"
else
  warn "not found, installing from astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer drops uv in ~/.local/bin, which may not be on PATH yet.
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || {
    printf '\n\033[31m✖\033[0m uv installed but not on PATH.\n'
    printf '  Add this to your shell profile and re-run:\n'
    printf '    export PATH="$HOME/.local/bin:$PATH"\n'
    exit 1
  }
  ok "installed ($(uv --version))"
fi

# -- 2. virtualenv -----------------------------------------------------------
step "virtualenv"
if [ -d .venv ]; then
  ok ".venv already exists"
else
  uv venv --python "$PYTHON_VERSION" .venv
  ok "created .venv on python $PYTHON_VERSION"
fi

# -- 3. the package ----------------------------------------------------------
step "package"
# Installs the one third-party dependency (`cryptography`, for client-side
# E2E encryption in ezchangelog/crypto.py) alongside the `ezcl` entry point,
# so the module resolves from any directory.
VIRTUAL_ENV="$HERE/.venv" uv pip install --quiet -e .
ok "ezchangelog installed (editable)"
ok "$(.venv/bin/python -c 'import ezchangelog; print("version " + ezchangelog.__version__)')"

# -- 3b. ezcl on PATH --------------------------------------------------------
step "ezcl on PATH"
# Claude Code's `!` shell and the plugin's hook shim both look for `ezcl` on
# PATH; the venv alone is invisible to them.
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/.venv/bin/ezcl" "$HOME/.local/bin/ezcl"
ln -sf "$HERE/.venv/bin/ezup" "$HOME/.local/bin/ezup"
if command -v ezcl >/dev/null 2>&1; then
  ok "~/.local/bin/ezcl -> .venv/bin/ezcl"
else
  warn '~/.local/bin is not on PATH; add: export PATH="$HOME/.local/bin:$PATH"'
fi

# -- 4. the Claude CLI -------------------------------------------------------
step "claude CLI"
if command -v claude >/dev/null 2>&1; then
  ok "found at $(command -v claude)"
else
  warn "not found — collection works, but the journal pipeline cannot run"
  warn "install it, then re-run this script to confirm"
fi

# -- 5. smoke test -----------------------------------------------------------
step "smoke test"
if .venv/bin/ezcl status >/dev/null 2>&1; then
  ok "ezcl runs"
else
  warn "ezcl did not run cleanly; try: .venv/bin/ezcl status"
fi

TRANSCRIPTS="$HOME/.claude/projects"
if [ -d "$TRANSCRIPTS" ]; then
  ok "$(find "$TRANSCRIPTS" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ') transcripts found"
else
  warn "no transcripts at $TRANSCRIPTS yet"
fi

# -- 6. sharing (interactive) ------------------------------------------------
# Only when a person is watching. Skip with --no-enroll or in a non-TTY (CI).
EZUP="$HERE/.venv/bin/ezup"

if [ "${1:-}" = "--no-enroll" ] || [ ! -t 0 ]; then
  step "sharing"
  ok "skipped (non-interactive). Enrol later: ezup device enroll --name YOU"
elif "$EZUP" token show >/dev/null 2>&1; then
  step "sharing"
  ok "this machine is already set up — leaving it as is"
else
  step "sharing"
  printf '  Enter an alias to enable sharing (blank to skip): '
  read -r alias
  if [ -z "$alias" ]; then
    ok "skipped. Enable later: ezup device enroll --name YOU"
  elif "$EZUP" device enroll --name "$alias"; then
    ok "set up as $alias"
    if "$EZUP" hook install >/dev/null 2>&1; then
      ok "recording hook installed (share a session with /ezup on)"
    else
      warn "could not install the hook; run: ezup hook install"
    fi
    printf '  Mint a token to share your sessions? [Y/n] '
    read -r want
    case "$want" in
      [nN]*) ok "skipped. Mint later: ezup token mint --name reader" ;;
      *)
        echo
        "$EZUP" token mint --name "$alias" || \
          warn "mint failed; try later: ezup token mint --name reader"
        ;;
    esac
  else
    warn "setup failed (store may be admin-gated). Ask the store owner to run"
    warn "ezup device mint --name $alias -- then here: ezup login <token> <id>"
  fi
fi

cat <<'DONE'

Ready.

  Share a session:   open Claude Code, then  /ezup on   (/ezup off to stop)
  Read the team:     ezup keyring add ezr_...  then  ezup pull
  Make a journal:    ./collect 7d              (or: ezup pull, for teammates)

DONE
