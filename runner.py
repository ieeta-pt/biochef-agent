"""How a workflow is executed, as a choice rather than a fact (#15).

E2 asks for a second Runner provider. This is the first one, and the seam that
makes a second possible.

The split is deliberate and is the whole point of the file. Running a workflow is
two things fused together in the version this replaces:

  policy    the timeout, and killing the whole process group rather than the
            child. Every provider needs this, and it is the part that was hard
            to get right -- killing only the child leaves the tool running and
            then blocks forever on the pipes the orphan still holds.
  strategy  which command actually gets launched. This is the only part a
            container provider replaces.

So `run` is written once, here, and a provider supplies `command`. A provider
that wants a container does not get to reinvent the timeout, and cannot quietly
lose the group-kill.
"""

import os
import signal
import subprocess
from typing import List, NamedTuple


class RunResult(NamedTuple):
    """What a run produced. A tuple, because the handler already unpacks three."""

    returncode: int
    stdout: str
    stderr: str


class Runner:
    """A way of executing the Snakefile in a workspace.

    Subclasses supply `command`. They inherit the timeout and the group-kill,
    which is the point.
    """

    name = "runner"

    def command(self, ws) -> List[str]:
        raise NotImplementedError

    def describe(self) -> str:
        """What this provider is, for an operator reading a log or an error."""
        return self.name

    def run(self, ws, timeout_s: int) -> RunResult:
        """Launch the command in its own process group and bound how long it lives.

        start_new_session puts the command and everything it spawns in one
        process group, and the timeout kills the GROUP. The pgid is captured
        before the first wait, because after the child is reaped getpgid raises.
        """
        process = subprocess.Popen(
            self.command(ws),
            cwd=ws.path, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pgid = os.getpgid(process.pid)
        try:
            out, err = process.communicate(timeout=timeout_s)
            return RunResult(process.returncode, out, err)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            out, err = process.communicate()
            return RunResult(-signal.SIGKILL, out or "", err or "")


class SubprocessRunner(Runner):
    """Snakemake on this host, in the workspace. What the service did before.

    -s and -d give snakemake the Snakefile and the working directory explicitly,
    which is what makes a per-run directory possible: relative paths in the rules
    resolve under -d, and the shell blocks run with that as their cwd, so the
    emitter's ./{bin} convention is unchanged.
    """

    name = "subprocess"

    def command(self, ws) -> List[str]:
        return ["snakemake", "--cores", "4",
                "-s", os.path.join(ws.path, "Snakefile"),
                "-d", ws.path]


PROVIDERS = {
    SubprocessRunner.name: SubprocessRunner,
}


def get_runner(name: str) -> Runner:
    """Resolve a provider by name, and refuse to start on one that is not there.

    Failing at startup rather than on the first request. A deployment that asked
    for a runner it does not have should not accept work and then fail every
    submission, and an operator who mistypes it should be told by the process
    that will not start, not by a 500 in someone else's log.
    """
    try:
        provider = PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"BIOCHEF_RUNNER={name!r} is not a runner. "
            f"Available: {', '.join(sorted(PROVIDERS))}."
        ) from None
    return provider()
