#!/bin/sh
# Run the whole suite. Stdlib unittest, no pytest, no network.
#
# The repo root is the discovery top level so `tests` imports as a package and
# `ezchangelog` resolves from the working tree -- an installed copy would let a
# stale wheel pass a test the source would fail.
set -eu

cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi

exec "$PY" -m unittest discover -s tests -t . -v "$@"
