#!/usr/bin/env bash
# Starts the UI, the second process next to run.sh. `--dev` fills in what a
# laptop has no TRE for: a fixed user on a loopback address, marked in red on
# every page.
set -euo pipefail

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate

# The same check as run.sh, and for the same reasons.
python -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' || {
    echo "This needs Python 3.11, 3.12 or 3.13; the venv has $(python --version 2>&1)." >&2
    echo "Remove the venv directory and re-run with one of those as python3 on PATH." >&2
    exit 1
}

pip install -r requirements.txt

if [ "${1:-}" = "--dev" ]; then
    # Outside the repository, because keys are refused inside it. Delete this
    # directory for a fresh seed; the times in the data count from the seeding.
    DEV_HOME="${BIOCHEF_UI_DEV_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/biochef-agent-dev}"
    mkdir -p "$DEV_HOME"
    export BIOCHEF_UI_IDENTITY=dev
    export BIOCHEF_UI_HOST="${BIOCHEF_UI_HOST:-127.0.0.1}"
    export BIOCHEF_UI_DEV_USER="${BIOCHEF_UI_DEV_USER:-j.silva}"
    export BIOCHEF_NODE_NAME="${BIOCHEF_NODE_NAME:-Example TRE (test)}"
    export BIOCHEF_PASSPORT_REQUIRE_VISA="${BIOCHEF_PASSPORT_REQUIRE_VISA:-ControlledAccessGrants}"
    export BIOCHEF_AUDIT_LOG="${BIOCHEF_AUDIT_LOG:-$DEV_HOME/audit.jsonl}"
    export BIOCHEF_DB_URL="${BIOCHEF_DB_URL:-sqlite:///$DEV_HOME/agent.db}"
    export BIOCHEF_NODE_SIGNING_KEY="$DEV_HOME/keys/node-signing.pem"
    export BIOCHEF_NODE_ENCRYPTION_KEY="$DEV_HOME/keys/node-encryption.pem"
    export BIOCHEF_EGRESS_MODE="${BIOCHEF_EGRESS_MODE:-airlock}"
    # A laptop runs steps on the host, which the isolation rule refuses unless it
    # is turned off; dev mode turns it off, and the agent audits that at start.
    export BIOCHEF_REQUIRE_NETWORK_ISOLATION="${BIOCHEF_REQUIRE_NETWORK_ISOLATION:-false}"
    export BIOCHEF_LOCAL_ROOT="${BIOCHEF_LOCAL_ROOT:-$DEV_HOME/data}"
    export BIOCHEF_DATA_SOURCES="${BIOCHEF_DATA_SOURCES:-upload,spooled,handedover,localpath}"
    export BIOCHEF_DATASET_BINDINGS_FROM_UI="${BIOCHEF_DATASET_BINDINGS_FROM_UI:-true}"
    mkdir -p "$BIOCHEF_LOCAL_ROOT"/cardio/2019_2024 "$BIOCHEF_LOCAL_ROOT"/sepsis/icu_v4 \
             "$BIOCHEF_LOCAL_ROOT"/micro/16s_2023 "$BIOCHEF_LOCAL_ROOT"/rare/pilot_v1
    [ -f "$BIOCHEF_NODE_SIGNING_KEY" ] || python -m node.keys init --out "$DEV_HOME/keys" >/dev/null
    python -m store migrate
    [ -f "$DEV_HOME/.seeded" ] || { python -m store.seed --fixture mock; touch "$DEV_HOME/.seeded"; }
elif [ -n "${1:-}" ]; then
    echo "usage: ./run-ui.sh [--dev]" >&2
    exit 2
fi

# python -m ui keeps uvicorn's proxy headers off (--no-proxy-headers); the
# identity check depends on the address the socket reports.
exec python -m ui
