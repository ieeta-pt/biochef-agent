"""E2's acceptance, run for real: both runners, same workflow, same outputs (#15).

    "The same workflow runs under the subprocess runner and the container
     runner with identical outputs."

Asserting only that is not enough on its own. A container runner that silently
fell back to running on the host would satisfy it perfectly, and that is exactly
the failure worth catching -- the whole point of E2 is that the step is NOT on
the host. So the workflow produces six things:

  result.txt      the actual work. Must be byte-for-byte identical.
  where.txt       which operating system the step ran on. Must DIFFER.
  neighbour.txt   whether another run's data on the host was readable. Must be
                  readable outside the container and hidden inside it.
  exec.txt        the output of a file executed OUT OF the workspace. Must run
                  under both.
  env_inheritance.txt  whether a harmless host variable was inherited. Must be
                       present outside and absent inside the container.

exec.txt exists because `./<bin>` is the only rule shape the emitter ever
produces -- convert.py builds every command as `cmd = [f"./{node.bin}"]` -- and
every other rule here runs something the image supplies. A container that binds
the workspace readable but cannot execute out of it would run no real workflow
at all, while a check made entirely of image-supplied tools reported green.

  network.txt  whether a step could open a connection, and which interfaces it
               could see. Must reach the network outside and see only loopback
               inside.

The runner is ubuntu; the image is debian. If where.txt matches between the two
runs, the container was never entered and the parity is meaningless.

neighbour.txt is the one that nearly went missing. Apptainer binds the host's
/tmp unless told not to, and a workspace is created in the system temp
directory whenever BIOCHEF_RUN_ROOT is unset -- the default. A container that
walls a tool off from /usr and /etc while leaving every other run's data
readable has isolated the wrong half, and no comparison of outputs would show
it. The sentinel stands in for another run's workspace.

Kept out of the pytest suite deliberately: it needs apptainer and it pulls an
image over the network, neither of which belongs in a unit test run on every
push. CI calls this directly.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner import (CONTAINER_IMAGE, ApptainerRunner, PodmanRunner, SubprocessRunner,
                    check_network)

IMAGE = os.getenv("BIOCHEF_PARITY_IMAGE") or CONTAINER_IMAGE
"""The image the service would actually use, not a copy of it.

This previously repeated the default as a literal of its own and claimed, in
this docstring, to be checking "the image people get". It was not. Changing the
service default would have left this pulling the old image and reporting green
while every real rule failed -- which is exactly the regression that has already
happened once here: alpine has no bash, snakemake runs each rule as `bash -c`,
and every rule failed with exit 255.

Reading runner.CONTAINER_IMAGE means a change to the default is a change to what
this check runs. BIOCHEF_PARITY_IMAGE still overrides it, for testing an image
before making it the default.

The image's os-release ID must differ from the runner's ("debian" against
"ubuntu") or the container-was-really-entered check below cannot tell them apart.
"""
TIMEOUT_S = int(os.getenv("BIOCHEF_PARITY_TIMEOUT", "600"))

STEP_PIDS = "128"
STEP_MEMORY = "256m"
STEP_MEMORY_BYTES = 256 * 1024 ** 2
ALLOCATE = 2 * STEP_MEMORY_BYTES
"""The podman limits, and what a step tries to allocate: twice the limit, which
the host can afford, so that only the limit can stop it."""

INPUT_TEXT = "the quick brown fox\njumps over the lazy dog\n" * 32

# Portable on purpose: this has to behave the same in the image's coreutils
# and the runner's, or a difference in the tools would be mistaken for a
# difference between the runners.
SNAKEFILE = '''
rule all:
    input:
        "result.txt",
        "where.txt",
        "neighbour.txt",
        "exec.txt",
        "env_inheritance.txt",
        "network.txt",
        "confined.txt"

rule confined:
    output:
        o_0="confined.txt"
    shell:
        "if touch /usr/biochef-probe 2>/dev/null; then echo writable; "
        "else echo read-only; fi > {output.o_0}; "
        "{{ cat /sys/fs/cgroup/pids.max 2>/dev/null || echo none; }} >> {output.o_0}; "
        "{{ cat /sys/fs/cgroup/memory.max 2>/dev/null || echo none; }} >> {output.o_0}"

rule network:
    output:
        o_0="network.txt"
    shell:
        "if timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null; "
        "then echo reachable; else echo unreachable; fi > {output.o_0}; "
        "tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | sed '/^lo$/d' "
        "| wc -l >> {output.o_0}"

rule work:
    input:
        i_0="input.txt"
    output:
        o_0="result.txt"
    shell:
        "tr 'a-z' 'A-Z' < {input.i_0} | sort | uniq -c | sed 's/^ *//' > {output.o_0}"

rule where:
    output:
        o_0="where.txt"
    shell:
        "if [ -r /etc/os-release ]; then . /etc/os-release; printf '%s\\\\n' \\"$ID\\"; "
        "else uname -s; fi > {output.o_0}"

rule neighbour:
    output:
        o_0="neighbour.txt"
    shell:
        "if [ -r __SENTINEL__ ]; then echo readable; else echo hidden; fi > {output.o_0}"

rule from_workspace:
    output:
        o_0="exec.txt"
    log:
        stdout="work.stdout", stderr="work.stderr"
    shell:
        "( ./tool > {output.o_0} ) > {log.stdout} 2> {log.stderr}"

rule env_inheritance:
    output:
        o_0="env_inheritance.txt"
    shell:
        "if printenv BIOCHEF_PARITY_ENV_PROBE >/dev/null; then echo inherited; "
        "else echo absent; fi > {output.o_0}"
'''


OVERRUN = f'''
rule overrun:
    output: "overrun.txt"
    shell: "head -c {ALLOCATE} /dev/zero | tail > /dev/null; echo survived > {{output}}"
'''


class _Workspace:
    def __init__(self, path):
        self.path = path


def _prepare(root, preamble, sentinel):
    ws = _Workspace(tempfile.mkdtemp(dir=root))
    with open(os.path.join(ws.path, "input.txt"), "w") as f:
        f.write(INPUT_TEXT)
    with open(os.path.join(ws.path, "Snakefile"), "w") as f:
        f.write(preamble
                + SNAKEFILE.replace("__SENTINEL__", shlex.quote(sentinel)))

    # Stands in for a tool binary. `./<bin>` is the ONLY rule shape the emitter
    # produces (convert.py: cmd = [f"./{node.bin}"]), and every rule above runs
    # something the image supplies instead -- so nothing here had ever asked the
    # container to execute a file out of the bound workspace.
    #
    # Placed the way workspace.place_executable places a real one, 0700 and all.
    # A script rather than a binary because that half needs no registry and no
    # network; ABI compatibility of a real catalogue binary with the image is a
    # separate question that CI cannot answer without credentials.
    tool = os.path.join(ws.path, "tool")
    with open(tool, "w") as f:
        f.write("#!/bin/sh\necho executed-from-workspace\n"
                "echo step-stderr-marker >&2\n")
    os.chmod(tool, 0o700)
    return ws


def _read(ws, name):
    with open(os.path.join(ws.path, name), "rb") as f:
        return f.read()


def main():
    if shutil.which("apptainer") is None and shutil.which("singularity") is None:
        print("FAIL: neither apptainer nor singularity is on PATH; "
              "this check cannot verify anything", file=sys.stderr)
        return 2
    if shutil.which("podman") is None:
        print("FAIL: podman is not on PATH; this check cannot verify anything",
              file=sys.stderr)
        return 2

    os.environ["BIOCHEF_PARITY_ENV_PROBE"] = "test-value"

    root = tempfile.mkdtemp(prefix="biochef-parity-")
    cache = os.path.join(root, "apptainer-cache")
    # Deliberately NOT created here. Snakemake makes the --apptainer-prefix
    # directory itself -- persistence/__init__.py runs makedirs over a list
    # that includes container_img_path -- and creating it here would hide a
    # production failure behind a CI convenience.

    # Stands in for another run's workspace. It sits beside the workspaces in
    # the system temp directory, which is exactly where make_workspace puts a
    # real run when BIOCHEF_RUN_ROOT is unset, i.e. by default.
    sentinel = os.path.join(root, "another-run-secret.txt")
    with open(sentinel, "w") as f:
        f.write("another run's data\n")

    runners = [
        ("subprocess", SubprocessRunner()),
        ("apptainer", ApptainerRunner(image=IMAGE, cache_dir=cache)),
        # Both settings, so that neither stops a step from working. The runner
        # has no SELinux, so this proves the flags and the isolation, not what
        # an enforcing host does with the labels.
        ("podman", PodmanRunner(image=IMAGE, selinux="disable", mode="rootless",
                                pids=STEP_PIDS, memory=STEP_MEMORY)),
        ("podman-keep", PodmanRunner(image=IMAGE, selinux="keep", mode="rootless",
                                     pids=STEP_PIDS, memory=STEP_MEMORY)),
    ]

    outcomes = {}
    for label, runner in runners:
        ws = _prepare(root, runner.snakefile_preamble(), sentinel)
        print(f"--- {label}: {' '.join(runner.command(ws))}", flush=True)
        result = runner.run(ws, timeout_s=TIMEOUT_S)
        if result.returncode != 0:
            print(f"FAIL: {label} exited {result.returncode}", file=sys.stderr)
            print(result.stderr[-4000:], file=sys.stderr)
            return 1
        outcomes[label] = (_read(ws, "result.txt"), _read(ws, "where.txt"),
                           _read(ws, "neighbour.txt"), _read(ws, "exec.txt"),
                           _read(ws, "env_inheritance.txt"),
                           _read(ws, "work.stdout"), _read(ws, "work.stderr"),
                           _read(ws, "network.txt"), _read(ws, "confined.txt"))
        print(f"    ran on: {outcomes[label][1].decode().strip()!r}   "
              f"other runs' data: {outcomes[label][2].decode().strip()!r}",
              flush=True)

    (plain_result, plain_where, plain_neighbour, plain_exec,
     plain_env_result, plain_log_out, plain_log_err,
     plain_network, plain_confined) = outcomes["subprocess"]
    (boxed_result, boxed_where, boxed_neighbour, boxed_exec,
     boxed_env_result, boxed_log_out, boxed_log_err,
     boxed_network, boxed_confined) = outcomes["apptainer"]

    failures = []
    if plain_result != boxed_result:
        failures.append(
            f"outputs differ:\n  subprocess: {plain_result[:200]!r}\n"
            f"  apptainer : {boxed_result[:200]!r}"
        )
    if plain_where.strip() == boxed_where.strip():
        failures.append(
            f"both runs report the same operating system "
            f"({plain_where.decode().strip()!r}), so the container was never "
            f"entered -- identical outputs prove nothing here"
        )

    # The isolation E2 is actually for. Apptainer binds the host's /tmp
    # unless told not to, and that is where a workspace lives by default, so
    # without --contain a containerised tool is walled off from /usr and /etc
    # while still able to read every other run's data. The container runs as
    # the same user, so a workspace's 0700 mode does not help.
    if plain_neighbour.strip() != b"readable":
        failures.append(
            "the sentinel was not readable even on the host, so this check "
            "cannot tell containment apart from a missing file"
        )
    if boxed_neighbour.strip() != b"hidden":
        failures.append(
            f"a containerised step could read another run's data on the host "
            f"({boxed_neighbour.decode().strip()!r}) -- the container isolates "
            f"the system but not the one thing that matters here"
        )

    # `./<bin>` is the only rule shape the emitter emits, so if a file cannot
    # be executed out of the bound workspace inside the container, no real
    # workflow runs at all -- while a check made of image-supplied tools stays
    # green.
    for label, got in (("subprocess", plain_exec), ("apptainer", boxed_exec)):
        if got.strip() != b"executed-from-workspace":
            failures.append(
                f"{label}: a file placed in the workspace did not execute "
                f"({got.strip()!r}); the emitter only ever produces ./<bin>"
            )

    for label, stdout, stderr in (("subprocess", plain_log_out, plain_log_err),
                                  ("apptainer", boxed_log_out, boxed_log_err)):
        if stdout or stderr.strip() != b"step-stderr-marker":
            failures.append(
                f"{label}: per-rule log capture failed "
                f"(stdout={stdout!r}, stderr={stderr!r})"
            )

    if plain_env_result.strip() != b"inherited":
        failures.append("the host-side step did not receive the test variable")
    if boxed_env_result.strip() != b"absent":
        failures.append("a containerised step inherited the test environment variable")

    # A tool that processed the data must not be able to send it anywhere. The
    # host-side run is what makes this a test: if it could not connect either,
    # "unreachable" inside would prove nothing about the container.
    plain_reach, plain_interfaces = plain_network.split()
    boxed_reach, boxed_interfaces = boxed_network.split()
    if plain_reach != b"reachable" or int(plain_interfaces) == 0:
        failures.append(
            f"the host-side step could not reach the network ({plain_network!r}), "
            f"so this check cannot tell isolation apart from a network that is down"
        )
    if boxed_reach != b"unreachable" or int(boxed_interfaces) != 0:
        failures.append(
            f"a containerised step had the network ({boxed_network!r}); it must "
            f"see only loopback"
        )

    # The same promises from the second container runner, under both SELinux
    # settings. The workflow is the same, so a difference here is a difference
    # between the runners.
    for label in ("podman", "podman-keep"):
        (pod_result, pod_where, pod_neighbour, pod_exec, pod_env, pod_log_out,
         pod_log_err, pod_network, pod_confined) = outcomes[label]
        if pod_result != plain_result:
            failures.append(f"{label}: outputs differ: {pod_result[:200]!r}")
        if pod_where.strip() == plain_where.strip():
            failures.append(f"{label}: steps ran on the host's operating system, so "
                            f"the container was never entered")
        if pod_neighbour.strip() != b"hidden":
            failures.append(f"{label}: a step could read another run's data on the "
                            f"host")
        if pod_exec.strip() != b"executed-from-workspace":
            failures.append(f"{label}: a file in the workspace did not execute "
                            f"({pod_exec.strip()!r})")
        if pod_log_out or pod_log_err.strip() != b"step-stderr-marker":
            failures.append(f"{label}: per-rule log capture failed "
                            f"(stdout={pod_log_out!r}, stderr={pod_log_err!r})")
        if pod_env.strip() != b"absent":
            failures.append(f"{label}: a step inherited the test environment variable")
        pod_reach, pod_interfaces = pod_network.split()
        if pod_reach != b"unreachable" or int(pod_interfaces) != 0:
            failures.append(f"{label}: a step had the network ({pod_network!r})")
        # The root filesystem and the limits in the step's own cgroup.
        if pod_confined.split() != [b"read-only", STEP_PIDS.encode(),
                                    str(STEP_MEMORY_BYTES).encode()]:
            failures.append(f"{label}: read-only root, limits and the memory kill: "
                            f"{pod_confined.split()!r}")

    # And the memory limit enforced: a step that allocates twice its limit does
    # not finish. A workflow of its own, because the kernel may kill the whole
    # step rather than only the allocating process.
    for label, runner in runners:
        if not label.startswith("podman"):
            continue
        ws = _Workspace(tempfile.mkdtemp(dir=root))
        with open(os.path.join(ws.path, "Snakefile"), "w") as f:
            f.write(runner.snakefile_preamble() + OVERRUN)
        outcome = runner.run(ws, timeout_s=TIMEOUT_S)
        if outcome.returncode == 0 or os.path.exists(os.path.join(ws.path, "overrun.txt")):
            failures.append(f"{label}: a step that allocated {ALLOCATE} bytes finished")
        elif "137" not in outcome.stderr:
            failures.append(f"{label}: the overrun failed, but not by being killed: "
                            f"{outcome.stderr[-1500:]!r}")

    # The check the agent runs at start (SPEC.md 14.1), through the real runners:
    # it must pass in their containers and fail on the host, with a probe the
    # host can reach.
    probe = ("1.1.1.1", 53)
    for label, runner in runners:
        result = check_network(runner, root=root, timeout_s=TIMEOUT_S, probe=probe,
                               force=True)
        if (label == "subprocess") == result.ok:
            failures.append(f"{label}: the network check at start said "
                            f"{result.ok}: {result.detail}")
        print(f"    network check, {label}: {result.ok}: {result.detail}", flush=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(f"OK: identical outputs ({len(plain_result)} bytes); the steps ran on "
          f"different systems ({plain_where.decode().strip()!r} vs "
          f"{boxed_where.decode().strip()!r}), so the container was real; "
          f"a file placed in the workspace executed under both; and another "
          f"run's data was readable on the host but hidden inside it; "
          f"the test environment variable was not inherited; and a step could "
          f"reach the network on the host but only loopback inside; and podman "
          f"kept every one of those promises too, with SELinux labels disabled "
          f"and kept, and with a read-only root filesystem, the limits applied and a memory "
          f"overrun killed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
