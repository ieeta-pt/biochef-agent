"""E2's acceptance, run for real: both runners, same workflow, same outputs (#15).

    "The same workflow runs under the subprocess runner and the container
     runner with identical outputs."

Asserting only that is not enough on its own. A container runner that silently
fell back to running on the host would satisfy it perfectly, and that is exactly
the failure worth catching -- the whole point of E2 is that the step is NOT on
the host. So the workflow produces two things:

  result.txt      the actual work. Must be byte-for-byte identical.
  where.txt       which operating system the step ran on. Must DIFFER.

The runner is ubuntu; the image is alpine. If where.txt matches between the two
runs, the container was never entered and the parity is meaningless.

Kept out of the pytest suite deliberately: it needs apptainer and it pulls an
image over the network, neither of which belongs in a unit test run on every
push. CI calls this directly.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runner import ApptainerRunner, SubprocessRunner

IMAGE = os.getenv("BIOCHEF_PARITY_IMAGE", "docker://alpine:3.20")
TIMEOUT_S = int(os.getenv("BIOCHEF_PARITY_TIMEOUT", "600"))

INPUT_TEXT = "the quick brown fox\njumps over the lazy dog\n" * 32

# Portable on purpose: this has to behave the same in busybox sh and in
# ubuntu's coreutils, or a difference in the tools would be mistaken for a
# difference between the runners.
SNAKEFILE = '''
rule all:
    input:
        "result.txt",
        "where.txt"

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
'''


class _Workspace:
    def __init__(self, path):
        self.path = path


def _prepare(root, preamble):
    ws = _Workspace(tempfile.mkdtemp(dir=root))
    with open(os.path.join(ws.path, "input.txt"), "w") as f:
        f.write(INPUT_TEXT)
    with open(os.path.join(ws.path, "Snakefile"), "w") as f:
        f.write(preamble + SNAKEFILE)
    return ws


def _read(ws, name):
    with open(os.path.join(ws.path, name), "rb") as f:
        return f.read()


def main():
    if shutil.which("apptainer") is None and shutil.which("singularity") is None:
        print("FAIL: neither apptainer nor singularity is on PATH; "
              "this check cannot verify anything", file=sys.stderr)
        return 2

    root = tempfile.mkdtemp(prefix="biochef-parity-")
    cache = os.path.join(root, "apptainer-cache")
    os.makedirs(cache, exist_ok=True)

    runners = [
        ("subprocess", SubprocessRunner()),
        ("apptainer", ApptainerRunner(image=IMAGE, cache_dir=cache)),
    ]

    outcomes = {}
    for label, runner in runners:
        ws = _prepare(root, runner.snakefile_preamble())
        print(f"--- {label}: {' '.join(runner.command(ws))}", flush=True)
        result = runner.run(ws, timeout_s=TIMEOUT_S)
        if result.returncode != 0:
            print(f"FAIL: {label} exited {result.returncode}", file=sys.stderr)
            print(result.stderr[-4000:], file=sys.stderr)
            return 1
        outcomes[label] = (_read(ws, "result.txt"), _read(ws, "where.txt"))
        print(f"    ran on: {outcomes[label][1].decode().strip()!r}", flush=True)

    plain_result, plain_where = outcomes["subprocess"]
    boxed_result, boxed_where = outcomes["apptainer"]

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

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(f"OK: identical outputs ({len(plain_result)} bytes), and the steps ran "
          f"on different systems ({plain_where.decode().strip()!r} vs "
          f"{boxed_where.decode().strip()!r}), so the container was real")
    return 0


if __name__ == "__main__":
    sys.exit(main())
