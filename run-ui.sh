#!/usr/bin/env bash
# Starts the UI, the second process next to run.sh. `--dev` fills in what a
# laptop has no TRE for: a fixed user on a loopback address, marked in red on
# every page.
set -euo pipefail

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate

python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "This needs Python 3.11 or newer; the venv has $(python --version 2>&1)." >&2
    echo "Remove the venv directory and re-run with a newer python3 on PATH." >&2
    exit 1
}

pip install -r requirements.txt

if [ "${1:-}" = "--dev" ]; then
    mkdir -p tmp
    export BIOCHEF_UI_IDENTITY=dev
    export BIOCHEF_UI_HOST="${BIOCHEF_UI_HOST:-127.0.0.1}"
    export BIOCHEF_UI_DEV_USER="${BIOCHEF_UI_DEV_USER:-dev.user}"
    export BIOCHEF_NODE_NAME="${BIOCHEF_NODE_NAME:-Development node}"
    export BIOCHEF_AUDIT_LOG="${BIOCHEF_AUDIT_LOG:-$PWD/tmp/dev-audit.jsonl}"
elif [ -n "${1:-}" ]; then
    echo "usage: ./run-ui.sh [--dev]" >&2
    exit 2
fi

# python -m ui keeps uvicorn's proxy headers off (--no-proxy-headers); the
# identity check depends on the address the socket reports.
exec python -m ui
