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
# The range is the CI matrix, and tests/test_python_guard.py fails if the two
# differ. 3.11 is the floor the pinned requirements impose: snakemake 9.21.0 and
# its interface plugins declare requires_python >=3.11. 3.14 is the newest CI
# tests; there immutables 0.21 has no wheel and pip builds it, so a C compiler
# is needed. On 3.15, pinned packages without wheels fail to build with pages of
# output that hide why.
python -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' || {
    echo "This needs Python 3.11, 3.12, 3.13 or 3.14; the venv has $(python --version 2>&1)." >&2
    echo "Rebuild the venv with one of those, for example: rm -rf venv && python3.13 -m venv venv && ./run.sh" >&2
    exit 1
}

pip install -r requirements.txt

fastapi run main.py
