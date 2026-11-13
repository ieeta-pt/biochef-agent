"""Rootful podman, run for real: what a step gets when podman runs as root.

Run by ci/podman-rootful.sh as the agent's user, inside a Debian 11 container
with that distribution's podman 3.0.1, after it has installed podman-step-root
with its sudo rule and configuration. It runs one workflow through the podman
runner in rootful mode and checks, from inside the step and from the host, the
promises the mode makes:

  the step runs as STEP_UID:STEP_GID, never as root and never as the agent
  the root filesystem is read-only, and /tmp is still writable
  the processes and memory limits are the ones in the configuration, and a step
  that allocates past the memory limit is killed
  no interface but loopback is up, and nothing outside can be reached
  another run's directory, beside this one, cannot be read
  the agent's environment does not reach the step
  a file placed in the workspace can be executed, as the emitter's ./<bin> needs
  what the step wrote is the agent's again afterwards, so the workspace can be
  deleted
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner import (PodmanRunner, _read_step_conf, PODMAN_STEP_CONF,  # noqa: E402
                    check_network)

SNAKEFILE = r'''
rule all:
    input: "who.txt", "ro.txt", "limits.txt", "network.txt",
           "neighbour.txt", "env.txt", "exec.txt"

rule who:
    output: "who.txt"
    shell: "id -u > {output}; id -g >> {output}"

rule ro:
    output: "ro.txt"
    shell:
        "if touch /usr/biochef-probe 2>/dev/null; then echo writable; "
        "else echo read-only; fi > {output}; "
        "if touch /tmp/biochef-probe; then echo tmp-writable; fi >> {output}"

rule limits:
    output: "limits.txt"
    shell: "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max > {output}"

rule network:
    output: "network.txt"
    shell:
        "if timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null; "
        "then echo reachable; else echo unreachable; fi > {output}; "
        "up=0; for i in /sys/class/net/*; do n=${{i##*/}}; [ -f \"$i/flags\" ] || continue; "
        "[ \"$n\" = lo ] && continue; "
        "f=$(cat \"$i/flags\"); if (( f & 1 )); then up=$((up+1)); fi; done; "
        "echo $up >> {output}"

rule neighbour:
    output: "neighbour.txt"
    shell: "if [ -r __SENTINEL__ ]; then echo readable; else echo hidden; fi > {output}"

rule env:
    output: "env.txt"
    shell:
        "if printenv BIOCHEF_PARITY_ENV_PROBE >/dev/null; then echo inherited; "
        "else echo absent; fi > {output}"

rule exec:
    output: "exec.txt"
    shell: "./tool > {output}"
'''


OVERRUN = r'''
rule overrun:
    output: "overrun.txt"
    shell: "head -c __ALLOCATE__ /dev/zero | tail > /dev/null; echo survived > {output}"
'''
"""A step that allocates twice its memory limit. The kernel may kill only the
allocating process or the whole step, so the step's own outcome is what is
checked: it must not finish."""


class _Workspace:
    def __init__(self, path):
        self.path = path


def _memory_bytes(size):
    return int(size[:-1]) * {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[size[-1]]


def main():
    failures = []
    conf = _read_step_conf(PODMAN_STEP_CONF, 0)
    root = conf["RUN_ROOT"]
    os.environ["BIOCHEF_PARITY_ENV_PROBE"] = "test-value"
    runner = PodmanRunner(image=conf["IMAGE"], mode="rootful", run_root=root)

    neighbour = tempfile.mkdtemp(dir=root)
    sentinel = os.path.join(neighbour, "another-run-secret.txt")
    with open(sentinel, "w") as f:
        f.write("another run's data\n")
    os.chmod(neighbour, 0o2770)
    os.chown(neighbour, -1, runner.step_gid)
    os.chmod(sentinel, 0o660)
    os.chown(sentinel, -1, runner.step_gid)

    ws = _Workspace(tempfile.mkdtemp(dir=root))
    allocate = str(2 * _memory_bytes(conf["MEMORY"]))
    with open(os.path.join(ws.path, "Snakefile"), "w") as f:
        f.write(runner.snakefile_preamble()
                + SNAKEFILE.replace("__SENTINEL__", sentinel))
    tool = os.path.join(ws.path, "tool")
    with open(tool, "w") as f:
        f.write("#!/bin/sh\necho executed-from-workspace\n")
    os.chmod(tool, 0o700)

    print(f"--- {runner.describe()}", flush=True)
    result = runner.run(ws, timeout_s=900)
    if result.returncode != 0:
        print(f"FAIL: the run exited {result.returncode}", file=sys.stderr)
        print(result.stderr[-6000:], file=sys.stderr)
        return 1

    def read(name):
        with open(os.path.join(ws.path, name)) as f:
            return f.read().split()

    uid, gid = read("who.txt")
    if (int(uid), int(gid)) != (runner.step_uid, runner.step_gid):
        failures.append(f"the step ran as {uid}:{gid}, not as STEP_UID:STEP_GID")
    if uid == "0" or int(uid) == os.getuid():
        failures.append(f"the step ran as {uid}, which is root or the agent")
    if read("ro.txt") != ["read-only", "tmp-writable"]:
        failures.append(f"root filesystem and /tmp: {read('ro.txt')}")
    if read("limits.txt") != [conf["PIDS"], str(_memory_bytes(conf["MEMORY"]))]:
        failures.append(f"the limits in the step's cgroup are {read('limits.txt')}, not "
                        f"PIDS={conf['PIDS']} and MEMORY={conf['MEMORY']}")
    if read("network.txt") != ["unreachable", "0"]:
        failures.append(f"a step had the network: {read('network.txt')}")
    if read("neighbour.txt") != ["hidden"]:
        failures.append("a step could read another run's directory")
    if read("env.txt") != ["absent"]:
        failures.append("a step inherited the agent's environment")
    if read("exec.txt") != ["executed-from-workspace"]:
        failures.append(f"a file in the workspace did not execute: {read('exec.txt')}")

    # after_run has given the workspace back: everything is the agent's again.
    strangers = []
    for top, dirs, files in os.walk(ws.path):
        for name in [*dirs, *files, ""]:
            path = os.path.join(top, name)
            if os.lstat(path).st_uid != os.getuid():
                strangers.append(path)
    if strangers:
        failures.append(f"not the agent's after the run: {strangers[:5]}")
    shutil.rmtree(ws.path)

    over = _Workspace(tempfile.mkdtemp(dir=root))
    with open(os.path.join(over.path, "Snakefile"), "w") as f:
        f.write(runner.snakefile_preamble() + OVERRUN.replace("__ALLOCATE__", allocate))
    outcome = runner.run(over, timeout_s=600)
    if outcome.returncode == 0 or os.path.exists(os.path.join(over.path, "overrun.txt")):
        failures.append(f"a step that allocated {allocate} bytes finished")
    elif "137" not in outcome.stderr:
        failures.append(f"the overrun failed, but not by being killed: "
                        f"{outcome.stderr[-1500:]}")
    shutil.rmtree(over.path)

    # The check the agent runs at start, through rootful podman.
    result = check_network(runner, root=root, timeout_s=600, probe=("1.1.1.1", 53))
    print(f"    network check: {result.ok}: {result.detail}", flush=True)
    if not result.ok:
        failures.append(f"the network check at start failed: {result.detail}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("OK: rootful podman ran the step as STEP_UID:STEP_GID with a read-only root "
          "filesystem, the configured limits, a memory overrun killed, no network, "
          "neither another run nor the agent's environment visible, and the workspace "
          "the agent's again afterwards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
