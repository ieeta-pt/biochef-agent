#!/usr/bin/env bash
# Does the documented local stack actually come up? (#19)
#
# E6's acceptance is that a new developer reaches a running stack by following
# the document alone. That is only checkable by following it, so this runs the
# same commands the walkthrough gives and then asks each service whether it is
# there.
#
# It asserts on the services rather than on docker's exit code. `compose up -d`
# succeeds as soon as the containers are CREATED, which is not the same as the
# agent being able to serve a request -- and the agent in particular logs into
# the registry at import, so it exits rather than waits if the registry is not
# ready.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
AGENT_PORT="${AGENT_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
DEADLINE="${DEADLINE:-180}"

fail() { echo "FAIL: $*" >&2; exit 1; }

wait_for() {
    local name=$1 url=$2 deadline=$((SECONDS + DEADLINE))
    while (( SECONDS < deadline )); do
        if curl -fsS -o /dev/null "$url" 2>/dev/null; then
            echo "  $name is answering at $url"
            return 0
        fi
        sleep 2
    done
    echo "--- $name never answered; last 40 lines of its log ---" >&2
    $COMPOSE logs --tail 40 "$name" >&2 || true
    fail "$name did not come up within ${DEADLINE}s"
}

echo "=== bringing the stack up ==="
# Not `set -e` straight through: when a dependency exits, compose fails here and
# the useful information is in the container's own log, not in compose's exit
# code. Show it before giving up.
if ! $COMPOSE up -d --build; then
    echo "--- compose could not bring the stack up; container logs follow ---" >&2
    $COMPOSE logs --no-color --tail 60 >&2 || true
    $COMPOSE ps -a >&2 || true
    fail "compose up did not succeed"
fi

echo "=== waiting for each service ==="
wait_for registry "http://127.0.0.1:${REGISTRY_PORT}/v2/"
wait_for agent    "http://127.0.0.1:${AGENT_PORT}/openapi.json"
wait_for frontend "http://127.0.0.1:${FRONTEND_PORT}/"

echo "=== the agent offers the endpoint the frontend calls ==="
curl -fsS "http://127.0.0.1:${AGENT_PORT}/openapi.json" \
    | grep -q '"/convert"' \
    || fail "the agent came up but does not serve /convert"
echo "  /convert is in the served OpenAPI document"

# Advertising a path in a schema and routing a request to it are different
# claims, and only the second is the developer's problem. An empty POST reaches
# the route and is rejected by validation -- 422, because both form fields are
# required -- which cannot happen unless the endpoint is really mounted. A 404
# here would mean the check above had passed vacuously.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${AGENT_PORT}/convert")
[ "$code" = "422" ] || fail "POST /convert returned $code, expected 422 (route not mounted?)"
echo "  POST /convert is routed and validates its input (422 on an empty body)"

echo "=== the agent can reach the registry it was configured with ==="
# The agent logs in at import, so it would not be answering at all if it could
# not reach the registry -- but say so explicitly, because that is the coupling
# a developer is most likely to get wrong when adapting this.
$COMPOSE exec -T agent python -c "
import os, urllib.request
url = 'http://' + os.environ['REGISTRY_URL'] + '/v2/'
with urllib.request.urlopen(url, timeout=10) as r:
    assert r.status in (200, 401), r.status
print('  agent reached', url)
"

echo "OK: registry, agent and frontend are all up, and the agent serves /convert"
