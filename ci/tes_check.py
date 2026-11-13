"""The TES runner against a TES server that runs as root, in containers.

CI starts tests/tes_support.py as root (so that what it returns is owned by root, as
a Funnel running as root would), installs tes-step-root and the one sudo rule as the
README says, and runs this as an ordinary user. It runs a workflow through
Snakemake and the real tes-step, and checks from inside the steps and from the
files:

  - the step ran in the image by digest, not on the host
  - another run's directory is not there, and the agent's environment is not
  - what the server returned was owned by root, and was the agent's after the
    helper had run, so retention can delete it
  - a run that times out has its task cancelled through the server (the workflow
    then checks that no container is left)

Not checked, because it is the server's and not the runner's: that the container
has no network. The stand-in starts its containers with --network none and says so;
a real server's setting is what the operator attests.

This is a stand-in for Funnel, not Funnel. It proves the exchange and what the
agent does around it; it does not prove any particular server's behaviour.
"""

import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tes  # noqa: E402
from runner import TesRunner  # noqa: E402

EXPECT_ROOT = os.environ.get("TES_CHECK_EXPECT_ROOT") == "1"
SECRET = "agent-only-secret-value-0451"

WORKFLOW = '''
rule all:
    input: "where.txt", "upper.txt"

rule where:
    output: "where.txt"
    shell:
        "( . /etc/os-release; echo os=$ID-$VERSION_ID ) > where.txt; "
        "echo user=$(id -u) >> where.txt; "
        "if [ -e {neighbour} ]; then echo neighbour=visible; else echo neighbour=hidden; fi >> where.txt; "
        "if env | grep -q {secret}; then echo env=inherited; else echo env=clean; fi >> where.txt"

rule upper:
    input: "where.txt"
    output: "upper.txt"
    shell: "tr a-z A-Z < where.txt > upper.txt"
'''

SLOW = '''
rule all:
    input: "never.txt"

rule slow:
    output: "never.txt"
    shell: "sleep 300; touch never.txt"
'''


class Workspace:
    def __init__(self, path):
        self.path = str(path)


def fail(message):
    print(f"FAIL: {message}")
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)
    print(f"ok: {message}")


def refused(client):
    """Whether a call to the server is answered 401 or 403."""
    try:
        client.get_list("biochef-")
    except tes.TesError as error:
        return "answered 401" in str(error) or "answered 403" in str(error)
    return False


def owner_of_returned_files(step_directory):
    seen = set()
    for top, dirs, files in os.walk(os.path.join(step_directory, "out")):
        for name in dirs + files:
            seen.add(os.lstat(os.path.join(top, name)).st_uid)
    return seen


def main():
    os.environ["BIOCHEF_AUDIT_SECRET"] = SECRET
    runner = TesRunner()
    print("runner:", runner.describe())
    if runner.settings.auth != "none":
        # The server wants credentials: without them, or with the wrong ones, it
        # must say no, or the runner's own token or password proves nothing.
        bare = runner.settings._replace(auth="none", token_file=None, user=None,
                                        password_file=None)
        check(refused(tes.Client(bare)), "the server refuses a request with no credentials")
        if runner.settings.auth == "basic":
            wrong = Path(tempfile.mkdtemp(prefix="biochef-wrong-")) / "password"
            wrong.write_text("not-the-synthetic-password\n")
            wrong.chmod(0o600)
            check(refused(tes.Client(runner.settings._replace(password_file=str(wrong)))),
                  "and one with the wrong password")
    staging = Path(runner.settings.staging)
    runs = Path(tempfile.mkdtemp(prefix="biochef-run-"))
    neighbour = staging / "neighbour-run"
    neighbour.mkdir(exist_ok=True)
    (neighbour / "secret.txt").write_text("another run's data\n")

    observed = {}
    real_give_back = tes.give_back

    def watching(settings, directory):
        # The first call for a directory is the one that follows the task.
        first = directory not in observed
        before = owner_of_returned_files(directory) if first else None
        real_give_back(settings, directory)
        if first:
            observed[directory] = (before, owner_of_returned_files(directory))

    tes.give_back = watching

    ws = runs / "biochef-run-ci_1"
    ws.mkdir()
    (ws / "Snakefile").write_text(
        runner.snakefile_preamble()
        + WORKFLOW.replace("{neighbour}", str(neighbour / "secret.txt"))
        .replace("{secret}", SECRET))
    result = runner.run(Workspace(ws), timeout_s=600)
    if result.returncode != 0:
        print(result.stdout, result.stderr)
    check(result.returncode == 0, f"the workflow ran through TES (exit {result.returncode})")
    where = dict(line.split("=", 1) for line in (ws / "where.txt").read_text().split())
    release = Path("/etc/os-release")
    fields = (dict(line.split("=", 1) for line in release.read_text().splitlines()
                   if "=" in line) if release.exists() else {})
    host_os = (f"{fields['ID']}-{fields.get('VERSION_ID', '').strip(chr(34))}"
               if "ID" in fields else sys.platform)
    check(where["os"] != host_os, f"the step ran in the image ({where['os']}), not on "
                                  f"the host ({host_os})")
    check(where["neighbour"] == "hidden", "another run's directory is not there")
    check(where["env"] == "clean", "the agent's environment is not there")
    check((ws / "upper.txt").read_text().startswith("OS="),
          "the second step read what the first one wrote")
    check(not (staging / ws.name).exists(), "staging is cleared at the end")

    # Snakemake runs tes-step as a child process, which this process cannot watch,
    # so one step is run here, in this process, with the helper watched.
    single = runs / "biochef-run-ci_3"
    single.mkdir()
    os.chdir(single)
    code = tes.main(["-c", "echo made > made.txt"], environ=runner.environment(),
                    out=io.StringIO(), err=io.StringIO())
    check(code == 0 and (single / "made.txt").read_text() == "made\n",
          "one step run in this process came back")
    if EXPECT_ROOT:
        before = set().union(*(pair[0] for pair in observed.values()))
        after = set().union(*(pair[1] for pair in observed.values()))
        check(before == {0}, f"the server returned files owned by root (owners {before})")
        check(after == {os.getuid()},
              f"the helper gave them back to the agent (owners {after})")
    os.chdir(runs)
    shutil.rmtree(single)
    foreign = [os.path.join(top, name) for top, dirs, files in os.walk(ws)
               for name in dirs + files
               if os.lstat(os.path.join(top, name)).st_uid != os.getuid()]
    check(not foreign, f"everything in the workspace is the agent's {foreign[:3]}")
    shutil.rmtree(ws)
    check(not ws.exists(), "retention can delete the workspace")

    slow = runs / "biochef-run-ci_2"
    slow.mkdir()
    (slow / "Snakefile").write_text(runner.snakefile_preamble() + SLOW)
    started = time.monotonic()
    result = runner.run(Workspace(slow), timeout_s=20)
    check(result.returncode != 0, "a run that times out fails")
    check(time.monotonic() - started < 120, "and does not wait for its task")
    check(not (staging / slow.name).exists(), "and its staging was cleared")

    # The server's own view, through the filter the runner cancels by.
    client = tes.Client(runner.settings)
    mine = client.get_list(tes.task_prefix(slow.name))
    check(mine and all(task.get("state") == "CANCELED" for task in mine),
          f"the server lists the timed-out run's task as CANCELED by name_prefix "
          f"({[task.get('state') for task in mine]})")
    check(client.get_list(tes.task_prefix("biochef-run-nothing_9")) == [],
          "a prefix that matches nothing lists nothing")
    first = client.get_list(tes.task_prefix("biochef-run-ci_1"))
    check(first and all(task.get("id") not in {each.get("id") for each in mine}
                        for task in first),
          "and another run's tasks are not in the timed-out run's list")
    shutil.rmtree(runs, ignore_errors=True)
    print("all checks passed")


if __name__ == "__main__":
    main()
