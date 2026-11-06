#!/usr/bin/env bash
set -euo pipefail

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate

# Checked after activation, so a venv built earlier on an older Python is caught
# too, and before pip, because on an unsupported interpreter the install fails
# first and buries this message under the resolver's output.
#
# 3.11 is the floor the pinned requirements impose: snakemake 9.21.0 and its
# interface plugins declare requires_python >=3.11. convert.py itself parses
# from 3.9 upward. 3.13 is the newest version CI tests, and on a newer one pip
# can end up compiling pinned packages and failing with pages of build output.
python -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' || {
    echo "This needs Python 3.11, 3.12 or 3.13; the venv has $(python --version 2>&1)." >&2
    echo "Remove the venv directory and re-run with one of those as python3 on PATH." >&2
    exit 1
}

pip install -r requirements.txt

fastapi run main.py
