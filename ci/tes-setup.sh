#!/usr/bin/env bash
# Sets up, on a CI runner, what the README says an operator sets up for the TES
# runner, and starts a TES server that runs as root. It is the stand-in of
# tests/tes_support.py, not Funnel: it speaks the same exchange, runs each task in a
# docker container with no network, and returns files owned by root, which is the
# case tes-step-root exists for.
#
# The agent is a user of its own, biochefagent, with the one sudo rule and nothing
# else. The CI runner's own user can sudo anything, so a check that "the rule allows
# nothing more" would mean nothing for it. Run as the runner's user, which creates
# that user and copies this checkout to /srv/biochef-agent-src for it.
#
# Then it checks that the sudo rule allows the helper and nothing else, and that
# the helper refuses what it must, each as the agent's user.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
STAGING=/srv/biochef-tes-staging
LIBEXEC=/usr/local/libexec/biochef-agent
HELPER=$LIBEXEC/tes-step-root
WORK=${RUNNER_TEMP:-/tmp}
AGENT=biochefagent
as_agent() { sudo -u "$AGENT" -H "$@"; }

sudo useradd --create-home --shell /bin/bash "$AGENT"
sudo rm -rf /srv/biochef-agent-src
sudo cp -r "$SRC" /srv/biochef-agent-src
sudo chown -R "$AGENT:$AGENT" /srv/biochef-agent-src
sudo install -d -o root -g root -m 0755 "$LIBEXEC" /etc/biochef-agent
sudo install -o root -g root -m 0755 "$SRC/tes-step-root" "$HELPER"
sudo install -d -o "$AGENT" -g "$AGENT" -m 0770 "$STAGING"
printf '# tes\nSTAGING_ROOT=%s\n' "$STAGING" | sudo tee /etc/biochef-agent/tes-step.conf >/dev/null
sudo chown root:root /etc/biochef-agent/tes-step.conf
sudo chmod 0644 /etc/biochef-agent/tes-step.conf
echo "$AGENT ALL=(root) NOPASSWD: $HELPER" | sudo tee /etc/sudoers.d/biochef-tes >/dev/null
sudo chmod 0440 /etc/sudoers.d/biochef-tes
sudo visudo -cf /etc/sudoers.d/biochef-tes

# The rule allows the helper, and not a shell or anything else.
as_agent sudo -n -l "$HELPER" >/dev/null
if as_agent sudo -n true 2>/dev/null; then
    echo "FAIL: the sudo rule allows more than the helper" >&2
    exit 1
fi

refuses() {
    local what=$1
    shift
    if out=$(as_agent sudo -n "$HELPER" "$@" 2>&1); then
        echo "FAIL: the helper accepted $what" >&2
        exit 1
    fi
    echo "ok: the helper refuses $what ($(echo "$out" | head -1))"
}
as_agent mkdir -p "$STAGING/run-a/step-1" "$STAGING/shallow"
as_agent ln -s "$STAGING/run-a" "$STAGING/link-run"
as_agent mkdir -p "/home/$AGENT/outside/step"
refuses "an unknown command" run "$STAGING/run-a/step-1"
refuses "no directory" finish
refuses "a relative path" finish run-a/step-1
refuses "a directory outside STAGING_ROOT" finish "/home/$AGENT/outside/step"
refuses "a directory one level down" finish "$STAGING/shallow"
refuses "a path through a symlink" finish "$STAGING/link-run/step-1"
refuses "a path with .." finish "$STAGING/run-a/../run-a/step-1"
as_agent sudo -n "$HELPER" finish "$STAGING/run-a/step-1"
echo "ok: the helper accepts a step directory of the agent's"
sudo rm -rf "$STAGING/run-a" "$STAGING/shallow" "$STAGING/link-run" "/home/$AGENT/outside"

# In a directory the agent can reach, which the runner's temp directory is not.
TOKEN=/etc/biochef-agent/tes-token
head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | sudo tee "$TOKEN" >/dev/null
sudo chown "$AGENT:$AGENT" "$TOKEN"
sudo chmod 0600 "$TOKEN"
sudo rm -f "$WORK/tes-ready"
sudo -b python3 /srv/biochef-agent-src/tests/tes_support.py --allow "$STAGING" --mode docker \
    --token-file "$TOKEN" --ready-file "$WORK/tes-ready" \
    >"$WORK/tes-server.log" 2>&1
for _ in $(seq 1 50); do
    [ -s "$WORK/tes-ready" ] && break
    sleep 0.2
done
[ -s "$WORK/tes-ready" ] || { cat "$WORK/tes-server.log"; echo "FAIL: no server" >&2; exit 1; }
{
    echo "BIOCHEF_TES_URL=$(cat "$WORK/tes-ready")"
    echo "BIOCHEF_TES_TOKEN_FILE=$TOKEN"
    echo "BIOCHEF_TES_STAGING=$STAGING"
    echo "BIOCHEF_TES_HELPER=$HELPER"
    echo "AGENT_USER=$AGENT"
} >> "${GITHUB_ENV:-/dev/stdout}"
echo "ok: a TES server running as root at $(cat "$WORK/tes-ready")"
