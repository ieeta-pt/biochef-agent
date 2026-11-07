#!/usr/bin/env bash
# Rootful podman 3.0.1, the version Debian 11 ships, set up the way the README
# says and then tested for real. Runs as root inside a privileged Debian 11
# container that has the repository at /src; CI starts it.
#
# It installs podman, podman-step-root, its configuration and its sudo rule,
# checks that the rule allows nothing else and that the script refuses what it
# must, and then runs ci/podman_rootful.py as the agent's user.
set -euo pipefail

# Debian 11's archives moved to archive.debian.org, the security updates too.
rm -f /etc/apt/sources.list.d/*
cat > /etc/apt/sources.list <<'EOF'
deb http://archive.debian.org/debian bullseye main
deb http://archive.debian.org/debian bullseye-updates main
deb http://archive.debian.org/debian-security bullseye-security main
EOF
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# catatonit is only a Recommends of podman, and --init needs it.
apt-get install -y -qq --no-install-recommends \
    podman=3.0.1+dfsg1-3+deb11u5 catatonit=0.1.5-2 sudo >/dev/null
dpkg-query -W podman crun conmon catatonit sudo

# Podman inside a container: vfs storage, because overlay on overlay does not
# mount, and cgroupfs, because there is no systemd. The processes move out of
# the root cgroup so its controllers can be handed down to the containers.
mkdir -p /etc/containers
printf '[storage]\ndriver = "vfs"\n' > /etc/containers/storage.conf
printf '[engine]\ncgroup_manager = "cgroupfs"\nevents_logger = "file"\n' \
    > /etc/containers/containers.conf
mkdir -p /sys/fs/cgroup/init
while read -r pid; do
    echo "$pid" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
done < /sys/fs/cgroup/cgroup.procs
sed 's/[^ ]\+/+&/g' /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
podman --version
podman info | grep -E 'cgroupVersion|cgroupManager|graphDriverName|^    name:'

# The agent's user, the steps' group it belongs to, and a step user that is
# neither root nor the agent.
groupadd -g 1600 biochef-steps
useradd -m -u 1500 -G biochef-steps biochef
STEP_UID=1601

HELPER=/usr/local/libexec/biochef-agent/podman-step-root
CONF=/etc/biochef-agent/podman-step.conf
RUNS=/var/lib/biochef/runs
python3 -m venv /opt/venv
/opt/venv/bin/pip install -q -r /src/requirements.txt
# The service's own default, as the parity check does, so that changing it
# changes what is tested here.
IMAGE=${BIOCHEF_PARITY_IMAGE:-$(cd /src && /opt/venv/bin/python -c \
    'import runner; print(runner.CONTAINER_IMAGE)')}
install -d -o root -g root -m 0755 /usr/local/libexec/biochef-agent /etc/biochef-agent
install -o root -g root -m 0755 /src/podman-step-root "$HELPER"
install -d -o biochef -g biochef-steps -m 0750 "$RUNS"
write_conf() {
    cat > "$CONF" <<EOF
RUN_ROOT=$RUNS
STEP_UID=${1:-$STEP_UID}
STEP_GID=1600
IMAGE=$IMAGE
PIDS=64
MEMORY=256m
SELINUX=disable
EOF
    chmod 0644 "$CONF"
}
write_conf
echo "biochef ALL=(root) NOPASSWD: $HELPER" > /etc/sudoers.d/biochef-agent
chmod 0440 /etc/sudoers.d/biochef-agent
visudo -cf /etc/sudoers.d/biochef-agent

podman pull -q "$IMAGE" >/dev/null

as_agent() {
    runuser -u biochef -- env -i HOME=/home/biochef PATH=/opt/venv/bin:/usr/bin:/bin "$@"
}
failed=0
refused() {
    local what=$1
    shift
    if out=$(as_agent "$@" 2>&1); then
        echo "FAIL: allowed, and must not be: $what" >&2
        failed=1
    else
        echo "refused, as it must be: $what (${out##*podman-step-root: })"
    fi
}

# The rule allows the script and nothing else.
refused "podman itself through sudo" sudo -n podman ps
refused "a shell through sudo" sudo -n bash -c id

# The script checks what the agent hands it.
ws=$(as_agent mktemp -d -p "$RUNS")
chgrp biochef-steps "$ws"
refused "a workspace outside RUN_ROOT" sudo -n "$HELPER" run /tmp true
refused "a path through .." sudo -n "$HELPER" run "$RUNS/../runs/${ws##*/}" true
as_agent ln -s "$ws" "$RUNS/link"
refused "a workspace named by a symlink" sudo -n "$HELPER" run "$RUNS/link" true
other=$(as_agent mktemp -d -p "$RUNS")
refused "a workspace outside the steps' group" sudo -n "$HELPER" run "$other" true
rootws=$(mktemp -d -p "$RUNS")
chgrp biochef-steps "$rootws"
refused "a workspace that is not the agent's" sudo -n "$HELPER" run "$rootws" true
refused "an unknown command" sudo -n "$HELPER" exec "$ws" true
chmod 0664 "$CONF"
refused "a configuration others can write" sudo -n "$HELPER" run "$ws" true
write_conf 1500
refused "steps as the agent's own user" sudo -n "$HELPER" run "$ws" true
write_conf 0
refused "steps as root" sudo -n "$HELPER" run "$ws" true
write_conf
echo "NETWORK=host" >> "$CONF"
refused "an unknown key" sudo -n "$HELPER" run "$ws" true
write_conf
[ "$failed" = 0 ] || exit 1

# Outside a container the agent can reach the network, or "unreachable" inside
# would prove nothing.
as_agent timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' ||
    { echo "FAIL: the host cannot reach the network, so isolation cannot be told apart" >&2; exit 1; }

as_agent env BIOCHEF_PODMAN_MODE=rootful BIOCHEF_PODMAN_HELPER="$HELPER" \
    BIOCHEF_RUN_ROOT="$RUNS" BIOCHEF_CONTAINER_IMAGE="$IMAGE" \
    python /src/ci/podman_rootful.py
