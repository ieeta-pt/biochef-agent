#!/usr/bin/env bash
# Sets up, on a CI runner, what the README says an operator sets up for the TES
# runner, and starts a TES server that runs as root. It is the stand-in of
# tests/tes_support.py, not Funnel: it speaks the same exchange, runs each task in a
# docker container with no network, and returns files owned by root, which is the
# case tes-step-root exists for.
#
# Then it checks that the sudo rule allows the helper and nothing else, and that
# the helper refuses what it must. Run as the ordinary user that will be the agent.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
STAGING=/srv/biochef-tes-staging
LIBEXEC=/usr/local/libexec/biochef-agent
HELPER=$LIBEXEC/tes-step-root
WORK=${RUNNER_TEMP:-/tmp}
ME=$(id -un)

sudo install -d -o root -g root -m 0755 "$LIBEXEC" /etc/biochef-agent
sudo install -o root -g root -m 0755 "$SRC/tes-step-root" "$HELPER"
sudo install -d -o "$ME" -g "$(id -gn)" -m 0770 "$STAGING"
printf '# tes\nSTAGING_ROOT=%s\n' "$STAGING" | sudo tee /etc/biochef-agent/tes-step.conf >/dev/null
sudo chown root:root /etc/biochef-agent/tes-step.conf
sudo chmod 0644 /etc/biochef-agent/tes-step.conf
echo "$ME ALL=(root) NOPASSWD: $HELPER" | sudo tee /etc/sudoers.d/biochef-tes >/dev/null
sudo chmod 0440 /etc/sudoers.d/biochef-tes
sudo visudo -cf /etc/sudoers.d/biochef-tes

# The rule allows the helper, and not a shell or anything else.
sudo -n -l "$HELPER" >/dev/null
if sudo -n true 2>/dev/null; then
    echo "FAIL: the sudo rule allows more than the helper" >&2
    exit 1
fi

refuses() {
    local what=$1
    shift
    if out=$(sudo -n "$HELPER" "$@" 2>&1); then
        echo "FAIL: the helper accepted $what" >&2
        exit 1
    fi
    echo "ok: the helper refuses $what ($(echo "$out" | head -1))"
}
mkdir -p "$STAGING/run-a/step-1" "$STAGING/shallow"
ln -s "$STAGING/run-a" "$STAGING/link-run"
mkdir -p "$WORK/outside/step"
refuses "an unknown command" run "$STAGING/run-a/step-1"
refuses "no directory" finish
refuses "a relative path" finish run-a/step-1
refuses "a directory outside STAGING_ROOT" finish "$WORK/outside/step"
refuses "a directory one level down" finish "$STAGING/shallow"
refuses "a path through a symlink" finish "$STAGING/link-run/step-1"
refuses "a path with .." finish "$STAGING/run-a/../run-a/step-1"
sudo -n "$HELPER" finish "$STAGING/run-a/step-1"
echo "ok: the helper accepts a step directory of the agent's"
rm -rf "$STAGING/run-a" "$STAGING/shallow" "$STAGING/link-run" "$WORK/outside"

umask 077
head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$WORK/tes-token"
chmod 0600 "$WORK/tes-token"
sudo rm -f "$WORK/tes-ready"
sudo -b python3 "$SRC/tests/tes_support.py" --allow "$STAGING" --mode docker \
    --token-file "$WORK/tes-token" --ready-file "$WORK/tes-ready" \
    >"$WORK/tes-server.log" 2>&1
for _ in $(seq 1 50); do
    [ -s "$WORK/tes-ready" ] && break
    sleep 0.2
done
[ -s "$WORK/tes-ready" ] || { cat "$WORK/tes-server.log"; echo "FAIL: no server" >&2; exit 1; }
{
    echo "BIOCHEF_TES_URL=$(cat "$WORK/tes-ready")"
    echo "BIOCHEF_TES_TOKEN_FILE=$WORK/tes-token"
    echo "BIOCHEF_TES_STAGING=$STAGING"
    echo "BIOCHEF_TES_HELPER=$HELPER"
} >> "${GITHUB_ENV:-/dev/stdout}"
echo "ok: a TES server running as root at $(cat "$WORK/tes-ready")"
