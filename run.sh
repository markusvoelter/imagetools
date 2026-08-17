#!/usr/bin/env bash
#
# Launch the Image Tools UI, making sure the virtualenv and all Python
# dependencies (and ffmpeg) are present first.
#
# Usage:   ./run.sh            # set up if needed, then launch the UI
#          PYTHON=python3.12 ./run.sh   # use a specific interpreter to build .venv
#          ./run.sh --help     # args are passed through to the UI launcher
#
# To force a dependency refresh: delete .venv/.deps-installed (or the whole .venv).

set -euo pipefail

# Resolve the directory this script lives in, so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
STAMP="$VENV_DIR/.deps-installed"

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

# 3. Install/update dependencies. Skipped when already done, unless
#    requirements.txt changed since the last install.
if [ ! -f "$STAMP" ] || [ "$REQ_FILE" -nt "$STAMP" ]; then
    echo "Installing Python dependencies (this can take a while the first time) ..."
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r "$REQ_FILE"
    touch "$STAMP"
fi

# 4. Verify Tkinter (the GUI toolkit) is usable in this Python build.
if ! "$VENV_PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "Error: Tkinter is not available in this Python build." >&2
    echo "  macOS:        install the python.org build, or 'brew install python-tk'" >&2
    echo "  Debian/Ubuntu: 'sudo apt install python3-tk'" >&2
    exit 1
fi

# 5. Warn (non-fatally) if ffmpeg is missing — the video tools need it, but
#    the image-only tools work without it.
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: 'ffmpeg' not found on PATH — the video tools will fail." >&2
    echo "         Install it with 'brew install ffmpeg' (macOS)" \
         "or your package manager." >&2
fi

# 6. Launch the UI, replacing this shell process. Any extra args pass through.
exec "$VENV_PY" "$SCRIPT_DIR/image_tools_ui.py" "$@"
