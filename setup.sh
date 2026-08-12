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
# No third-party dependencies; this is for the `ezcl` entry point and so the
# module resolves from any directory.
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

cat <<'DONE'

Ready. Start with:

  ./collect              every project, last 7 days
  ./collect 30d          every project, last 30 days
  ./collect ~/code/proj  one project, last 7 days

The ./collect script uses .venv automatically. To get `ezcl` on your PATH
in this shell instead:

  source .venv/bin/activate

DONE
