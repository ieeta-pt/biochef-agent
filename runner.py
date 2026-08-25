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


def _kill_group(pgid):
    """End a process group, tolerating one that has already gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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

    def run(self, ws, timeout_s: int, on_start=None, on_finish=None) -> RunResult:
        """Launch the command in its own process group and bound how long it lives.

        start_new_session puts the command and everything it spawns in one
        process group, and the timeout kills the GROUP. The pgid is captured
        before the first wait, because after the child is reaped getpgid raises.

        That capture is belt and braces rather than strictly necessary:
        start_new_session makes the child a group leader, so its pgid always
        equals its pid. Replacing os.getpgid(process.pid) with process.pid is an
        equivalent change and no test can tell them apart -- noted so the next
        person does not spend time proving it, and kept because it says what is
        meant rather than relying on that identity.
        """
        process = subprocess.Popen(
            self.command(ws),
            cwd=ws.path, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pgid = os.getpgid(process.pid)
        # Handed out so something other than the timeout can end this run.
        # Cancellation needs precisely the lever the timeout already pulls, and
        # a caller that has the group id can pull it without this class growing
        # a notion of why a run is being stopped.
        #
        # Taken back the moment the child is reaped, which matters more than it
        # sounds. A process group id is a number the kernel is free to reissue
        # once the group is empty, so a caller still holding it after the run
        # has ended is holding a loaded weapon aimed at whoever gets that number
        # next. killpg only reaches a group LEADER, so the likely victim is
        # another run of this same service -- every one is a leader by
        # construction, and their creation rate rises with load.
        if on_start is not None:
            on_start(pgid)
        try:
            out, err = process.communicate(timeout=timeout_s)
            return RunResult(process.returncode, out, err)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            out, err = process.communicate()
            # The real code, not a constant. This used to return -SIGKILL
            # literally, which reported the signal we meant to send rather than
            # what happened -- so changing the signal, or the process dying some
            # other way, still claimed SIGKILL. It also made the timeout test
            # unable to tell the two apart.
            return RunResult(process.returncode, out or "", err or "")
        except BaseException:
            # Any other way out -- a broken pipe, an interpreter shutdown, a
            # KeyboardInterrupt -- and the child has NOT been reaped. The
            # finally below is about to forget its group id, which would leave
            # the tool running with nothing able to stop it, against a workspace
            # perform_run is on its way to deleting.
            #
            # Demonstrated by making communicate() raise OSError: the group was
            # still alive and the only handle to it had just been cleared.
            _kill_group(pgid)
            # Reaped as well as killed. SIGKILL ends the processes but leaves
            # this one's direct child a zombie until it is waited for, and a
            # zombie still occupies the process group -- so the group would
            # outlive everything in it, which is both a leak and a number that
            # cannot be reissued. CI caught this on Linux where the timing
            # differs from a laptop's.
            try:
                process.wait(timeout=10)
            except Exception:                    # noqa: BLE001
                pass
            raise
        finally:
            # Reached by every path, and by now the group is either reaped or
            # killed above. An earlier version of this comment said "both paths
            # reach here, and both have reaped the child", which was true of the
            # two paths it named and false of every other.
            if on_finish is not None:
                on_finish()


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

It must contain bash. Snakemake runs each rule as `bash -c` inside the
container, so an image without it fails every rule with exit 255 and an error
that names the shell command rather than the missing shell. Found in CI: alpine
was the obvious choice for a small image and cannot work at all. debian slim is
about 30 MB more and does.
"""

APPTAINER_CACHE = os.path.realpath(
    os.getenv("BIOCHEF_APPTAINER_CACHE", "apptainer-cache"))
"""Where pulled images live, shared by every run.

Outside the workspace for the same reason the tool cache is: a workspace is
created and destroyed per run, so an image cached inside one would be pulled
again every time.
"""

APPTAINER_ARGS = os.getenv("BIOCHEF_APPTAINER_ARGS", "--contain")
"""Extra flags for the apptainer invocation itself.

--contain by default, and the default is the point. Apptainer binds the host's
/tmp and /var/tmp into the container unless told not to, and a run's workspace
is created in the system temp directory when BIOCHEF_RUN_ROOT is unset -- which
is the default. Without this, a containerised tool would be isolated from the
host's /usr and /etc and /home while still being able to read every other run's
workspace, which for a service whose reason to exist is keeping one dataset away
from another is the wrong half to get right. The container runs as the same
user, so the 0700 mode on a workspace does not help.

Emptying this variable turns containment off, for an operator who needs the host
/tmp visible and knows what that means.
"""

_IMAGE_SHAPE = re.compile(r"\A[A-Za-z0-9/][A-Za-z0-9._:/@+-]{0,255}\Z")
"""Characters a container reference may contain.

The leading "/" is allowed so an absolute path to a local .sif can reach the
second check below. Without it that branch was unreachable -- the shape test
rejected every absolute path before the local-image rule ever ran.
"""

_IMAGE_SCHEMES = ("docker://", "oras://", "library://", "shub://",
                  "http://", "https://")

_LOCAL_IMAGE = re.compile(r"\A/[A-Za-z0-9._/+-]{1,255}\.(sif|simg)\Z")
"""An image already on disk, named absolutely.

Absolute on purpose. Snakemake decides what a container value means with
is_local_file(url): anything without a scheme is a local FILE, and its path is
resolved against the working directory -- which for us is the run's workspace,
where the client's uploads are written. So a relative value does not merely fail,
it quietly means "use a file out of the run directory as the container image".
An operator typing `ubuntu` would get that rather than an error.
"""


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
    if not (image.startswith(_IMAGE_SCHEMES) or _LOCAL_IMAGE.match(image)):
        raise ValueError(
            f"BIOCHEF_CONTAINER_IMAGE={image!r} has no scheme. Snakemake reads "
            f"a value without one as a local image FILE, resolved against the "
            f"run's own directory, so this would silently mean something other "
            f"than what it looks like. Use one of "
            f"{', '.join(_IMAGE_SCHEMES)} or an absolute path to a .sif."
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

    def __init__(self, image: str = None, cache_dir: str = None,
                 apptainer_args: str = None):
        self.image = CONTAINER_IMAGE if image is None else image
        self.cache_dir = APPTAINER_CACHE if cache_dir is None else cache_dir
        self.apptainer_args = (APPTAINER_ARGS if apptainer_args is None
                               else apptainer_args)
        # Fail here rather than when the Snakefile is written, so a bad image
        # stops the process from starting instead of failing every submission.
        self._literal = _image_literal(self.image)

    def describe(self) -> str:
        return f"{self.name} ({self.image})"

    def snakefile_preamble(self) -> str:
        return f"container: {self._literal}\n"

    def command(self, ws) -> List[str]:
        argv = ["snakemake", "--cores", "4",
                "-s", os.path.join(ws.path, "Snakefile"),
                "-d", ws.path,
                "--software-deployment-method", "apptainer",
                "--apptainer-prefix", self.cache_dir]
        if self.apptainer_args:
            # One argv element with an "=", not two. The value starts with a
            # dash, so as a separate element argparse reads it as another
            # option and snakemake exits 2 with "--apptainer-args: expected one
            # argument" and a page of usage -- which reads like a snakemake
            # problem rather than a quoting one.
            argv.append(f"--apptainer-args={self.apptainer_args}")
        return argv


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
