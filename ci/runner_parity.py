"""E2's acceptance, run for real: both runners, same workflow, same outputs (#15).

    "The same workflow runs under the subprocess runner and the container
     runner with identical outputs."

Asserting only that is not enough on its own. A container runner that silently
fell back to running on the host would satisfy it perfectly, and that is exactly
the failure worth catching -- the whole point of E2 is that the step is NOT on
the host. So the workflow produces four things:

  result.txt      the actual work. Must be byte-for-byte identical.
  where.txt       which operating system the step ran on. Must DIFFER.
  neighbour.txt   whether another run's data on the host was readable. Must be
                  readable outside the container and hidden inside it.
  exec.txt        the output of a file executed OUT OF the workspace. Must run
                  under both.

exec.txt exists because `./<bin>` is the only rule shape the emitter ever
produces -- convert.py builds every command as `cmd = [f"./{node.bin}"]` -- and
every other rule here runs something the image supplies. A container that binds
the workspace readable but cannot execute out of it would run no real workflow
at all, while a check made entirely of image-supplied tools reported green.

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

from runner import CONTAINER_IMAGE, ApptainerRunner, SubprocessRunner

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
        "exec.txt"

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
    shell:
        "./tool > {output.o_0}"
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
        f.write("#!/bin/sh\necho executed-from-workspace\n")
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
                           _read(ws, "neighbour.txt"), _read(ws, "exec.txt"))
        print(f"    ran on: {outcomes[label][1].decode().strip()!r}   "
              f"other runs' data: {outcomes[label][2].decode().strip()!r}",
              flush=True)

    plain_result, plain_where, plain_neighbour, plain_exec = outcomes["subprocess"]
    boxed_result, boxed_where, boxed_neighbour, boxed_exec = outcomes["apptainer"]

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

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print(f"OK: identical outputs ({len(plain_result)} bytes); the steps ran on "
          f"different systems ({plain_where.decode().strip()!r} vs "
          f"{boxed_where.decode().strip()!r}), so the container was real; "
          f"a file placed in the workspace executed under both; and another "
          f"run's data was readable on the host but hidden inside it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
