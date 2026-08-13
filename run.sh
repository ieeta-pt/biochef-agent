#!/usr/bin/env bash
set -euo pipefail

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

# Checked against the interpreter that will actually run the agent, and after
# activation rather than before, so a venv built earlier on an older Python is
# caught too. convert.py needs 3.12: it is what the pinned requirements were
# resolved against.
python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
    echo "This needs Python 3.12 or newer; the venv has $(python --version 2>&1)." >&2
    echo "Remove the venv directory and re-run with a newer python3 on PATH." >&2
    exit 1
}

fastapi run main.py
