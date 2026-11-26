"""SingularityCE, run for real, as a user that is not root (SPEC.md, section 14.3).

The parity check's workflow, through the singularity runner and through the host,
with the binary named by BIOCHEF_APPTAINER_BINARY. The same promises as apptainer:
the same result, the step not on the host, another run's data hidden, the agent's
environment not inherited, a file in the workspace executed, and no network. Then
the network check the agent runs at start: it must pass through singularity and
fail on the host.

CI runs it twice, with the setuid starter the package installs and with
`allow setuid = no`, where SingularityCE uses unprivileged user namespaces.
Neither run is as root.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from runner import SingularityRunner, SubprocessRunner, check_network  # noqa: E402
from runner_parity import IMAGE, TIMEOUT_S, _prepare, _read  # noqa: E402


def main():
    if os.geteuid() == 0:
        print("FAIL: run this as a user that is not root", file=sys.stderr)
        return 2
    os.environ["BIOCHEF_PARITY_ENV_PROBE"] = "test-value"
    root = tempfile.mkdtemp(prefix="biochef-singularity-")
    sentinel = os.path.join(root, "another-run-secret.txt")
    with open(sentinel, "w") as f:
        f.write("another run's data\n")
    boxed = SingularityRunner(image=IMAGE, cache_dir=os.path.join(root, "cache"))
    print(f"--- {boxed.describe()}", flush=True)

    outcomes = {}
    for label, runner in (("host", SubprocessRunner()), ("singularity", boxed)):
        ws = _prepare(root, runner.snakefile_preamble(), sentinel)
        result = runner.run(ws, timeout_s=TIMEOUT_S)
        if result.returncode != 0:
            print(f"FAIL: {label} exited {result.returncode}", file=sys.stderr)
            print(result.stderr[-4000:], file=sys.stderr)
            return 1
        outcomes[label] = {name: _read(ws, name).strip() for name in (
            "result.txt", "where.txt", "neighbour.txt", "exec.txt",
            "env_inheritance.txt", "network.txt")}

    host, box = outcomes["host"], outcomes["singularity"]
    failures = []
    if host["result.txt"] != box["result.txt"]:
        failures.append("the outputs differ")
    if host["where.txt"] == box["where.txt"]:
        failures.append(f"both ran on {host['where.txt']!r}: the container was not entered")
    if (host["neighbour.txt"], box["neighbour.txt"]) != (b"readable", b"hidden"):
        failures.append(f"another run's data: host {host['neighbour.txt']!r}, "
                        f"singularity {box['neighbour.txt']!r}")
    if box["exec.txt"] != b"executed-from-workspace":
        failures.append(f"a file in the workspace did not execute: {box['exec.txt']!r}")
    if (host["env_inheritance.txt"], box["env_inheritance.txt"]) != (b"inherited", b"absent"):
        failures.append("the environment probe was not inherited on the host only")
    host_reach, host_up = host["network.txt"].split()
    box_reach, box_up = box["network.txt"].split()
    if host_reach != b"reachable" or int(host_up) == 0:
        failures.append(f"the host could not reach the network ({host['network.txt']!r}), "
                        f"so isolation cannot be told apart")
    if box_reach != b"unreachable" or int(box_up) != 0:
        failures.append(f"a singularity step had the network ({box['network.txt']!r})")

    probe = ("1.1.1.1", 53)
    for label, runner, wanted in (("singularity", boxed, True),
                                  ("host", SubprocessRunner(), False)):
        result = check_network(runner, root=root, timeout_s=TIMEOUT_S, probe=probe,
                               force=True)
        print(f"    network check, {label}: {result.ok}: {result.detail}", flush=True)
        if result.ok is not wanted:
            failures.append(f"the network check through {label} said {result.ok}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("OK: SingularityCE, as a user that is not root, gave the same result off the "
          "host, hid another run's data and the agent's environment, executed a file "
          "from the workspace and had no network; the network check passed through it "
          "and failed on the host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
