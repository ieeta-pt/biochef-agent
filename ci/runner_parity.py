"""E2's acceptance, run for real: both runners, same workflow, same outputs (#15).

    "The same workflow runs under the subprocess runner and the container
     runner with identical outputs."

Asserting only that is not enough on its own. A container runner that silently
fell back to running on the host would satisfy it perfectly, and that is exactly
the failure worth catching -- the whole point of E2 is that the step is NOT on
the host. So the workflow produces three things:

  result.txt      the actual work. Must be byte-for-byte identical.
  where.txt       which operating system the step ran on. Must DIFFER.
  neighbour.txt   whether another run's data on the host was readable. Must be
                  readable outside the container and hidden inside it.

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

from runner import ApptainerRunner, SubprocessRunner

IMAGE = os.getenv("BIOCHEF_PARITY_IMAGE", "docker://debian:stable-slim")
"""The service default, deliberately: this checks the image people get.

Started as alpine, for size. Snakemake runs each rule as `bash -c` inside
the container and alpine has no bash, so every rule failed with exit 255.
Its os-release ID is "debian" against the runner's "ubuntu", so the two
still tell each other apart.
"""
TIMEOUT_S = int(os.getenv("BIOCHEF_PARITY_TIMEOUT", "600"))

INPUT_TEXT = "the quick brown fox\njumps over the lazy dog\n" * 32

# Portable on purpose: this has to behave the same in the image's coreutils
# and the runner's, or a difference in the tools would be mistaken for a
# difference between the runners.
SNAKEFILE = '''
rule all:
    input:
        "result.txt",
        "where.txt",
        "neighbour.txt"

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
                           _read(ws, "neighbour.txt"))
        print(f"    ran on: {outcomes[label][1].decode().strip()!r}   "
              f"other runs' data: {outcomes[label][2].decode().strip()!r}",
              flush=True)

    plain_result, plain_where, plain_neighbour = outcomes["subprocess"]
    boxed_result, boxed_where, boxed_neighbour = outcomes["apptainer"]

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

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(f"OK: identical outputs ({len(plain_result)} bytes); the steps ran on "
          f"different systems ({plain_where.decode().strip()!r} vs "
          f"{boxed_where.decode().strip()!r}), so the container was real; and "
          f"another run's data was readable on the host but hidden inside it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
