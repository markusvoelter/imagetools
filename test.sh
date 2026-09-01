#!/usr/bin/env bash
#
# Run the Image Tools test suite, making sure the virtualenv and all Python
# dependencies (including the test-only ones) are present first.
#
# Usage:   ./test.sh                  # set up if needed, then run all tests
#          ./test.sh -m "not slow"    # extra args pass through to pytest
#          ./test.sh tests/unit -q    # e.g. run one directory quietly
#          PYTHON=python3.12 ./test.sh   # use a specific interpreter to build .venv
#
# To force a dependency refresh: delete .venv/.dev-deps-installed (or the whole .venv).

set -euo pipefail

# Resolve the directory this script lives in, so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQ_FILE="$SCRIPT_DIR/requirements-dev.txt"
STAMP="$VENV_DIR/.dev-deps-installed"

# 1. Find a Python 3 interpreter to bootstrap the venv (only used if .venv
#    does not yet exist). Override with the PYTHON env var.
PYTHON_BIN="${PYTHON:-python3}"

# 2. Create the virtualenv if it is missing.
if [ ! -x "$VENV_PY" ]; then
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Error: '$PYTHON_BIN' not found. Install Python 3" \
             "(e.g. 'brew install python') or set PYTHON=/path/to/python3." >&2
        exit 1
    fi
    echo "Creating virtual environment in .venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# 3. Install/update dependencies (runtime + test). Skipped when already done,
#    unless requirements-dev.txt changed since the last install.
if [ ! -f "$STAMP" ] || [ "$REQ_FILE" -nt "$STAMP" ]; then
    echo "Installing test dependencies (this can take a while the first time) ..."
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r "$REQ_FILE"
    touch "$STAMP"
fi

# 4. Warn (non-fatally) if ffmpeg is missing — the ffmpeg-marked tests will be
#    skipped automatically, but the video tools themselves need it.
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Note: 'ffmpeg' not found on PATH — ffmpeg-marked tests will be skipped." >&2
fi

# 5. Run the test suite, replacing this shell process. Any extra args pass
#    through to pytest.
exec "$VENV_PY" -m pytest "$@"
