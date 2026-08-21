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

import json
import os
import re
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

    def snakefile_preamble(self) -> str:
        """Lines this provider needs at the top of the Snakefile, if any.

        The Snakefile is the emitter's. A provider that needs something in it --
        a container directive, say -- says so here rather than the emitter
        growing a notion of how the workflow will be run. That keeps the two
        independent, which is not only tidiness: the emitter and the runner are
        on different branches of this stack and would otherwise have to land
        together.
        """
        return ""

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


CONTAINER_IMAGE = os.getenv("BIOCHEF_CONTAINER_IMAGE", "docker://debian:stable-slim")
"""The image every step runs in.

A plain base image, because the tool binary is not baked in -- it is already in
the workspace, which snakemake binds as the working directory, so the image only
has to be able to execute it. That keeps one image for the whole catalogue
instead of one per tool, and keeps the ORAS registry as the single place a tool
comes from.
"""

APPTAINER_CACHE = os.path.realpath(
    os.getenv("BIOCHEF_APPTAINER_CACHE", "apptainer-cache"))
"""Where pulled images live, shared by every run.

Outside the workspace for the same reason the tool cache is: a workspace is
created and destroyed per run, so an image cached inside one would be pulled
again every time.
"""

_IMAGE_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")


def _image_literal(image: str) -> str:
    """Encode the image as a Python string literal for the Snakefile.

    The Snakefile is Python source, not a config file, so an image name reaches
    the interpreter as code. json.dumps produces a correctly escaped literal,
    and the shape check refuses anything that is not a container reference in
    the first place. Either alone would do; both, because this repository has
    already shipped one injection that survived a quoting function (#43).
    """
    if not _IMAGE_SHAPE.match(image):
        raise ValueError(
            f"BIOCHEF_CONTAINER_IMAGE={image!r} is not a container reference"
        )
    return json.dumps(image)


class ApptainerRunner(Runner):
    """Every step in a container, via snakemake's own deployment mechanism.

    Apptainer rather than docker, and not only because snakemake has no
    docker-per-rule mode. Apptainer pulls docker:// images, runs without a
    daemon and without root, and is what is actually available on the HPC
    systems this is meant to reach (F4) -- so the same workflow and the same
    image description work on a server and on a cluster.

    The container is requested once, globally, rather than per rule. Verified
    against snakemake 9.21: a global directive is honoured -- with one present
    snakemake refuses to start unless apptainer is available, and without it the
    identical flags run to completion.
    """

    name = "apptainer"

    def __init__(self, image: str = None, cache_dir: str = None):
        self.image = CONTAINER_IMAGE if image is None else image
        self.cache_dir = APPTAINER_CACHE if cache_dir is None else cache_dir
        # Fail here rather than when the Snakefile is written, so a bad image
        # stops the process from starting instead of failing every submission.
        self._literal = _image_literal(self.image)

    def describe(self) -> str:
        return f"{self.name} ({self.image})"

    def snakefile_preamble(self) -> str:
        return f"container: {self._literal}\n"

    def command(self, ws) -> List[str]:
        return ["snakemake", "--cores", "4",
                "-s", os.path.join(ws.path, "Snakefile"),
                "-d", ws.path,
                "--software-deployment-method", "apptainer",
                "--apptainer-prefix", self.cache_dir]


PROVIDERS = {
    SubprocessRunner.name: SubprocessRunner,
    ApptainerRunner.name: ApptainerRunner,
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
