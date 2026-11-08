"""How a workflow is executed, as a choice rather than a fact (#15).

E2 asks for a second Runner provider. This is the first one, and the seam that
makes a second possible.

The split is the whole point of the file. Running a workflow is two things:

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

import copy
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import List, NamedTuple

import tes
from steplogs import TailBuffer


def _kill_group(pgid):
    """End a process group, tolerating one that has already gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


ENFORCED = "enforced"
"""The agent applies it to every step, and a step it cannot apply it to fails."""
ATTESTED = "attested"
"""The operator says the backend applies it; the agent cannot check."""
NONE = "none"
"""Nobody applies it."""

GUARANTEES = ("network", "filesystem", "environment")
"""What a runner says about each step: whether it can reach the network, whether
it sees more than its own workspace and image, and whether it inherits the
agent's environment."""


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

    @property
    def guarantees(self) -> dict:
        """network, filesystem and environment, each enforced, attested or none."""
        return {aspect: NONE for aspect in GUARANTEES}

    @property
    def isolates_network(self) -> bool:
        """Whether the agent itself keeps a step from anything beyond loopback.

        Only an enforced guarantee counts. An attested one is someone's word that
        nobody here checks, and the rule that reads this exists for the case where
        nobody does.
        """
        return self.guarantees["network"] == ENFORCED

    def command(self, ws) -> List[str]:
        raise NotImplementedError

    def with_image(self, image: str) -> "Runner":
        """A runner like this one for a step that names its own image.

        `image` is `repository@sha256:<digest>`, with no scheme. A runner that
        runs steps in the one image it was configured with, or in no image at
        all, refuses a different one, and says so.
        """
        raise ValueError(f"the {self.name} runner cannot run an image chosen by "
                         f"the task")

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

    umask = -1
    """The umask the command starts with; -1 keeps the agent's."""

    def environment(self):
        """The command's environment, or None for the agent's own."""
        return None

    def before_run(self, ws) -> None:
        """Anything the workspace needs before the command starts."""

    def after_run(self, ws) -> None:
        """Anything the group kill cannot reach. Called on every way out of `run`."""

    def run(self, ws, timeout_s: int, on_start=None, on_finish=None,
            on_line=None) -> RunResult:
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
        self.before_run(ws)
        process = subprocess.Popen(
            self.command(ws),
            cwd=ws.path, start_new_session=True, umask=self.umask,
            env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        pgid = os.getpgid(process.pid)
        # Handed out so something other than the timeout can end this run.
        # Cancellation needs precisely the lever the timeout already pulls, and
        # a caller that has the group id can pull it without this class growing
        # a notion of why a run is being stopped.
        #
        # Taken back when runner cleanup finishes, which matters more than it sounds.
        # A process group id is a number the kernel can reissue after the group
        # disappears, so a caller still holding it can signal a later group.
        # The id is the leader's pid, but killpg targets the whole group even
        # after the original leader exits.
        # Read lines as they arrive so the caller can report progress before
        # the process exits. communicate() drains both pipes too, but returns
        # buffered output only after the process has finished.
        #
        # Two threads, one per stream, because draining only one of them is the
        # deadlock communicate() exists to avoid: a tool that fills the other
        # pipe's buffer blocks forever waiting for someone to read it.
        # Bounded, so a chatty tool cannot decide how much memory the agent
        # uses. Nothing is lost that would have survived being recorded: the
        # store keeps MAX_LOG_BYTES of a stream either way.
        collected = {"stdout": TailBuffer(), "stderr": TailBuffer()}
        reader_errors = []

        def pump(stream, name):
            try:
                for line in stream:
                    collected[name].append(line)
                    if on_line is not None:
                        try:
                            on_line(name, line)
                        except Exception:            # noqa: BLE001
                            # A defect in reporting must not take the run with
                            # it. The line is still collected either way.
                            pass
            except BaseException as error:  # noqa: BLE001
                reader_errors.append(error)
            finally:
                try:
                    stream.close()
                except Exception:                    # noqa: BLE001
                    pass

        readers = [
            threading.Thread(target=pump, args=(process.stdout, "stdout"),
                             daemon=True),
            threading.Thread(target=pump, args=(process.stderr, "stderr"),
                             daemon=True),
        ]
        reader_streams = dict(zip(readers, (process.stdout, process.stderr)))
        started_readers = []

        try:
            if on_start is not None:
                on_start(pgid)
            for reader in readers:
                reader.start()
                started_readers.append(reader)
            # Pass the configured value unchanged to the parent wait (among
            # other things, this keeps provider-level timeout observation
            # precise), then spend only the remaining budget draining pipes.
            deadline = time.monotonic() + timeout_s
            process.wait(timeout=timeout_s)
            returncode = process.returncode
            for reader in started_readers:
                reader.join(timeout=max(0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in started_readers):
                # A descendant can keep a pipe open after the parent exits.
                # Treat unfinished reading as a timeout, even if the parent
                # succeeded. Cleanup stays bounded if a child left the group
                # or a line callback is still blocked.
                _kill_group(pgid)
                process.wait()
                for reader in started_readers:
                    reader.join(timeout=10)
                if returncode == 0:
                    returncode = -signal.SIGKILL
            if reader_errors:
                raise reader_errors[0]
        except subprocess.TimeoutExpired:
            _kill_group(pgid)
            process.wait()
            for reader in started_readers:
                reader.join(timeout=10)
            returncode = process.returncode
            if returncode == 0:
                returncode = -signal.SIGKILL
        except BaseException:
            # Any other way out -- a broken pipe, an interpreter shutdown, a
            # KeyboardInterrupt -- and the child has NOT been reaped. The
            # finally below is about to forget its group id, which would leave
            # the tool running with nothing able to stop it, against a workspace
            # perform_run is on its way to deleting.
            #
            # Demonstrated by making wait() raise OSError: the group was
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
            for reader in started_readers:
                reader.join(timeout=10)
            raise
        finally:
            self.after_run(ws)
            # Reached by every path, and by now the group is either reaped
            # normally or killed above -- including the paths that raise, which
            # is why the kill is in an except rather than only in the timeout.
            #
            for reader in readers:
                if reader not in started_readers:
                    stream = reader_streams[reader]
                    try:
                        stream.close()
                    except Exception:                # noqa: BLE001
                        pass
            if on_finish is not None:
                on_finish()

        # The local, not process.returncode. Both branches of this merge were
        # guarding the same line against different mistakes and the one below
        # already covers both: it takes the process's real code -- so a run that
        # had already exited 3 does not come back claiming the signal we sent --
        # and substitutes only when that code is 0 after we killed the group,
        # because reading that overran the deadline did not produce a success.
        return RunResult(returncode,
                         collected["stdout"].text(),
                         collected["stderr"].text())


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

APPTAINER_ARGS = os.getenv("BIOCHEF_APPTAINER_ARGS",
                           "--contain --cleanenv --net --network none")
"""Extra flags for the apptainer invocation itself.

--contain by default, and the default is the point. Apptainer binds the host's
/tmp and /var/tmp into the container unless told not to, and a run's workspace
is created in the system temp directory when BIOCHEF_RUN_ROOT is unset -- which
is the default. Without this, a containerised tool would be isolated from the
host's /usr and /etc and /home while still being able to read every other run's
workspace, which for a service whose reason to exist is keeping one dataset away
from another is the wrong half to get right. The container runs as the same
user, so the 0700 mode on a workspace does not help.

--cleanenv stops ordinary Agent environment variables, including registry
credentials, being inherited by the tool process.

--net --network none gives each step a network namespace with only loopback in
it. A tool that processed a patient cohort must not be able to send it anywhere,
and the difference between the two is the whole reason a TRE exists. On a host
where apptainer refuses this, runs fail rather than carry on with the network:
isolation that quietly stops applying is worse than a run that stops.

None of this isolates /proc. Emptying this variable turns all of it off, for an
operator who needs host access and knows what that means.
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


def _container_binary(value, runner_name, expected):
    """BIOCHEF_APPTAINER_BINARY, checked to be what the runner says it is.

    Snakemake has no setting for the binary: it takes `apptainer` if one is on
    PATH and `singularity` otherwise, and apptainer's packages install a
    `singularity` that is apptainer. So the binary is named, absolutely, and its
    own --version decides whether it is the one the runner was asked for.
    """
    path = (os.getenv("BIOCHEF_APPTAINER_BINARY", "") if value is None
            else value).strip()
    if not os.path.isabs(path):
        raise ValueError(
            f"BIOCHEF_APPTAINER_BINARY={path!r}: BIOCHEF_RUNNER={runner_name} needs "
            f"the absolute path of the {runner_name} binary, and it has no default"
        )
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise ValueError(f"BIOCHEF_APPTAINER_BINARY={path!r} is not an executable file")
    try:
        said = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=30, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"BIOCHEF_APPTAINER_BINARY={path!r} did not run: "
                         f"{error}") from None
    if not said.startswith(expected):
        raise ValueError(
            f"BIOCHEF_APPTAINER_BINARY={path!r} says {said[:80]!r}, which is not "
            f"{runner_name}: BIOCHEF_RUNNER={runner_name} expects "
            f"{expected.strip()!r}"
        )
    return path, said


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
    version_prefix = "apptainer version "

    def __init__(self, image: str = None, cache_dir: str = None,
                 apptainer_args: str = None, binary: str = None):
        self.image = CONTAINER_IMAGE if image is None else image
        self.cache_dir = APPTAINER_CACHE if cache_dir is None else cache_dir
        self.apptainer_args = (APPTAINER_ARGS if apptainer_args is None
                               else apptainer_args)
        # Fail here rather than when the Snakefile is written, so a bad image
        # stops the process from starting instead of failing every submission.
        self._literal = _image_literal(self.image)
        self.binary, self.version = _container_binary(binary, self.name,
                                                      self.version_prefix)
        self._bin_dir = None

    def describe(self) -> str:
        return f"{self.name} ({self.image}, {self.version})"

    def environment(self):
        """PATH with the configured binary first, as the only `apptainer` found.

        Snakemake looks for `apptainer` before `singularity`, so a link of that
        name, first on PATH, is what it runs, whichever binary it points to.
        SingularityCE says "singularity-ce version", which snakemake accepts.
        The directory is the agent's own, made once, 0700.
        """
        if self._bin_dir is None:
            made = tempfile.mkdtemp(prefix="biochef-bin-")
            os.symlink(self.binary, os.path.join(made, "apptainer"))
            self._bin_dir = made
        return {**os.environ,
                "PATH": self._bin_dir + os.pathsep + os.environ.get("PATH", "")}

    def with_image(self, image: str) -> "ApptainerRunner":
        other = copy.copy(self)
        self.environment()          # the directory with the link, made once and shared
        other._bin_dir = self._bin_dir
        other.image = f"docker://{image}"
        other._literal = _image_literal(other.image)
        return other

    @property
    def guarantees(self) -> dict:
        """Read from the flags actually passed, so emptying them shows here.

        --containall is --contain and --cleanenv, among others.
        """
        words = self.apptainer_args.split()
        network = ("--net" in words and "--network" in words
                   and words[words.index("--network") + 1:][:1] == ["none"])
        contained = "--contain" in words or "--containall" in words
        clean = "--cleanenv" in words or "--containall" in words
        return {"network": ENFORCED if network else NONE,
                "filesystem": ENFORCED if contained else NONE,
                "environment": ENFORCED if clean else NONE}

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


class SingularityRunner(ApptainerRunner):
    """The same runner with SingularityCE, for a host that has it and not apptainer.

    The flags are the same. Read in SingularityCE 4.6.0: with --net --network
    none the network namespace is made and nothing is set up in it, and a user
    who is not root may always ask for that network and no other one unless
    singularity.conf allows it. CI runs it as a user that is not root, with the
    setuid starter and without it.
    """

    name = "singularity"
    version_prefix = "singularity-ce version "


PODMAN_SELINUX = ("disable", "keep")
PODMAN_MODES = ("rootless", "rootful")

PODMAN_STEP_CONF = "/etc/biochef-agent/podman-step.conf"
"""podman-step-root's configuration, at a path fixed in that script.

Not a setting on purpose: under rootful podman this file decides who a step runs
as and what it may use, so the agent, which is not trusted with root, must not be
able to point the script at another one.
"""

_PIDS_SHAPE = re.compile(r"\A[1-9][0-9]{0,6}\Z")
_MEMORY_SHAPE = re.compile(r"\A[1-9][0-9]{0,9}[kmg]\Z")
_ID_SHAPE = re.compile(r"\A[1-9][0-9]{0,9}\Z")
_ROOT_SHAPE = re.compile(r"\A/[A-Za-z0-9._/-]*[A-Za-z0-9._-]\Z")

_STEP_CONF_KEYS = ("RUN_ROOT", "STEP_UID", "STEP_GID", "IMAGE", "PIDS", "MEMORY",
                   "SELINUX")


def _owned_by(path, owner):
    """Owned by `owner` and writable by nobody else, the path and its directory."""
    for each in (path, os.path.dirname(path)):
        info = os.stat(each, follow_symlinks=False)
        if info.st_uid != owner or info.st_mode & 0o022:
            return False
    return True


def _read_step_conf(path, owner):
    """The values podman-step-root will use, read with the same rules it applies."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"{path} is missing; rootful podman needs it, see the README")
    if not _owned_by(path, owner):
        raise ValueError(f"{path} and its directory must be owned by root and "
                         f"writable by no one else")
    values = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream.read().splitlines():
            if not line or line.startswith("#"):
                continue
            key, equals, value = line.partition("=")
            if not equals or key not in _STEP_CONF_KEYS:
                raise ValueError(f"{path}: not a known KEY=value line: {line!r}")
            if key in values:
                raise ValueError(f"{path}: {key} appears twice")
            values[key] = value
    missing = [key for key in _STEP_CONF_KEYS if key not in values]
    if missing:
        raise ValueError(f"{path} has no {', '.join(missing)}")
    for key, shape in (("RUN_ROOT", _ROOT_SHAPE), ("STEP_UID", _ID_SHAPE),
                       ("STEP_GID", _ID_SHAPE), ("PIDS", _PIDS_SHAPE),
                       ("MEMORY", _MEMORY_SHAPE)):
        if not shape.match(values[key]):
            raise ValueError(f"{path}: {key}={values[key]!r} is not valid")
    if values["SELINUX"] not in PODMAN_SELINUX:
        raise ValueError(f"{path}: SELINUX must be disable or keep")
    return values


def _required(name, value, shape, what):
    value = (os.getenv(name, "") if value is None else value).strip()
    if not shape.match(value):
        raise ValueError(f"{name}={value!r}: it must be {what}, and has no default")
    return value


class PodmanRunner(Runner):
    """Every step in a podman container, with no network.

    Snakemake has no podman deployment method, so the seam is the shell its rules
    run under: the preamble makes podman-step that shell, and the step's command
    goes into a container from there. Snakemake itself stays on the host, as it
    does under apptainer, and the Snakefile does not change otherwise.

    The group kill reaches the podman client and not the container, which belongs
    to podman's own monitor, so `after_run` removes whatever a killed run left.

    BIOCHEF_PODMAN_MODE has no default, because the two are different hosts.
    `rootless` runs podman as the agent's user, and each step keeps that user.
    `rootful` runs podman as root, and the agent still does not: it calls
    podman-step-root through one sudo rule, and that script takes who a step runs
    as, the image, the limits and SELinux from its own configuration, which only
    root can change. Under root, `--userns=keep-id` would not keep the agent's
    user: podman 3.0.1 turns it into the host's user namespace with uid 0
    (pkg/specgen/generate/namespaces.go), so a step would run as root on the
    host. A rootful step runs as STEP_UID:STEP_GID instead, a user that is not
    the agent's, in a group the agent belongs to, so the agent can still read
    what the steps write.

    BIOCHEF_PODMAN_SELINUX has no default either. `disable` runs steps without
    SELinux separation, so the workspace mount works on any host without
    relabelling it; `keep` leaves podman's labelling on, and on an enforcing host
    the workspace must already carry a label containers may use.

    Every step has a read-only root filesystem and a limit on processes and on
    memory, with no swap past it. The limits have no default: what a tool needs
    depends on the catalogue and on the host.
    """

    name = "podman"

    def __init__(self, image: str = None, selinux: str = None, mode: str = None,
                 pids: str = None, memory: str = None, helper: str = None,
                 step_conf: str = PODMAN_STEP_CONF, run_root: str = None,
                 trusted_owner: int = 0):
        self.image = CONTAINER_IMAGE if image is None else image
        self._literal = _image_literal(self.image)
        if not self.image.startswith("docker://"):
            raise ValueError(
                f"BIOCHEF_CONTAINER_IMAGE={self.image!r} is not one podman can pull "
                f"under this runner; it needs a docker:// reference"
            )
        self.mode = (os.getenv("BIOCHEF_PODMAN_MODE", "") if mode is None
                     else mode).strip()
        if self.mode not in PODMAN_MODES:
            raise ValueError(
                f"BIOCHEF_PODMAN_MODE={self.mode!r}: it must be rootless or "
                f"rootful, and has no default. rootless runs podman as the agent's "
                f"user; rootful runs it as root through podman-step-root and one "
                f"sudo rule, with each step as a dedicated user"
            )
        if os.geteuid() == 0:
            raise ValueError(
                "the agent must not run as root under the podman runner: as root, "
                "every step would run as root on the host. Use rootful mode, which "
                "reaches root only through podman-step-root"
            )
        if self.mode == "rootless":
            self._rootless(selinux, pids, memory)
        else:
            self._rootful(selinux, pids, memory, helper, step_conf, run_root,
                          trusted_owner)

    def _rootless(self, selinux, pids, memory):
        self.selinux = (os.getenv("BIOCHEF_PODMAN_SELINUX", "") if selinux is None
                        else selinux).strip()
        if self.selinux not in PODMAN_SELINUX:
            raise ValueError(
                f"BIOCHEF_PODMAN_SELINUX={self.selinux!r}: it must be one of "
                f"{', '.join(PODMAN_SELINUX)}, and has no default. disable runs "
                f"steps without SELinux separation and mounts the workspace as it "
                f"is; keep leaves podman's labelling on, which on an SELinux host "
                f"needs the workspace labelled for containers (container_file_t)"
            )
        self.pids = _required("BIOCHEF_STEP_PIDS", pids, _PIDS_SHAPE,
                              "the most processes a step may have, a positive number")
        self.memory = _required("BIOCHEF_STEP_MEMORY", memory, _MEMORY_SHAPE,
                                "the most memory a step may use, such as 4g")
        self.helper = None
        self.step_uid = self.step_gid = None

    def _rootful(self, selinux, pids, memory, helper, step_conf, run_root,
                 trusted_owner):
        self.helper = (os.getenv("BIOCHEF_PODMAN_HELPER", "") if helper is None
                       else helper).strip()
        if not os.path.isabs(self.helper):
            raise ValueError(
                f"BIOCHEF_PODMAN_HELPER={self.helper!r}: rootful podman needs the "
                f"absolute path of podman-step-root as installed for sudo, and it "
                f"has no default"
            )
        if (os.path.islink(self.helper) or not os.path.isfile(self.helper)
                or not _owned_by(self.helper, trusted_owner)):
            raise ValueError(
                f"BIOCHEF_PODMAN_HELPER={self.helper!r} must be a file owned by "
                f"root, in a directory owned by root, neither writable by anyone "
                f"else, or the agent could change what runs as root"
            )
        conf = _read_step_conf(step_conf, trusted_owner)
        self.step_uid = int(conf["STEP_UID"])
        self.step_gid = int(conf["STEP_GID"])
        if self.step_uid == os.getuid():
            raise ValueError(f"{step_conf}: STEP_UID must not be the agent's user")
        if self.step_gid not in os.getgroups():
            raise ValueError(
                f"{step_conf}: the agent's user must be in group STEP_GID="
                f"{self.step_gid}, or it cannot read what the steps write"
            )
        root = (os.getenv("BIOCHEF_RUN_ROOT", "") if run_root is None
                else run_root).strip()
        if not root or os.path.realpath(root) != conf["RUN_ROOT"]:
            raise ValueError(
                f"BIOCHEF_RUN_ROOT={root!r} must be set, and be RUN_ROOT="
                f"{conf['RUN_ROOT']} of {step_conf}: podman-step-root runs only "
                f"workspaces directly under it"
            )
        if conf["IMAGE"] != self.image:
            raise ValueError(
                f"BIOCHEF_CONTAINER_IMAGE={self.image!r} is not IMAGE="
                f"{conf['IMAGE']!r} of {step_conf}, which is what the steps would "
                f"actually run in"
            )
        # One place for each value. A setting the agent also has must agree with
        # the file, or the Node card would show what the steps do not get.
        for name, given, key in (("BIOCHEF_PODMAN_SELINUX", selinux, "SELINUX"),
                                 ("BIOCHEF_STEP_PIDS", pids, "PIDS"),
                                 ("BIOCHEF_STEP_MEMORY", memory, "MEMORY")):
            value = (os.getenv(name, "") if given is None else given).strip()
            if value and value != conf[key]:
                raise ValueError(
                    f"{name}={value!r} disagrees with {key}={conf[key]!r} of "
                    f"{step_conf}; under rootful podman the file decides, so leave "
                    f"it unset"
                )
        self.selinux = conf["SELINUX"]
        self.pids = conf["PIDS"]
        self.memory = conf["MEMORY"]
        try:
            allowed = subprocess.run(["sudo", "-n", "-l", self.helper],
                                     capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"rootful podman needs sudo: {error}") from None
        if allowed.returncode != 0:
            raise ValueError(
                f"sudo does not let this user run {self.helper} without a "
                f"password; rootful podman needs exactly that rule, see the README"
            )
        # Group-writable for the steps' group, and nobody else, in what snakemake
        # creates while the run goes on.
        self.umask = 0o007

    def with_image(self, image: str) -> "PodmanRunner":
        wanted = f"docker://{image}"
        if self.mode == "rootful" and wanted != self.image:
            raise ValueError(
                f"rootful podman runs the one IMAGE of its configuration "
                f"({self.image}), which only root can change, and not {wanted}")
        other = copy.copy(self)
        other.image = wanted
        other._literal = _image_literal(wanted)
        return other

    @property
    def guarantees(self) -> dict:
        """All three, fixed in podman-step and podman-step-root with no setting."""
        return {aspect: ENFORCED for aspect in GUARANTEES}

    def describe(self) -> str:
        if self.mode == "rootful":
            return (f"{self.name} rootful ({self.image}, steps as "
                    f"{self.step_uid}:{self.step_gid})")
        return f"{self.name} ({self.image})"

    def snakefile_preamble(self) -> str:
        step = os.path.join(os.path.dirname(os.path.abspath(__file__)), "podman-step")
        settings = {"BIOCHEF_PODMAN_MODE": self.mode}
        if self.mode == "rootful":
            settings["BIOCHEF_PODMAN_HELPER"] = self.helper
        else:
            settings.update({"BIOCHEF_PODMAN_IMAGE": self.image,
                             "BIOCHEF_PODMAN_SELINUX": self.selinux,
                             "BIOCHEF_STEP_PIDS": self.pids,
                             "BIOCHEF_STEP_MEMORY": self.memory})
        lines = "".join(f"_os.environ[{json.dumps(key)}] = {json.dumps(value)}\n"
                        for key, value in settings.items())
        # bash's strict prefix is what snakemake would have set, and it sets that
        # only for an executable named bash.
        return (f"import os as _os\n"
                f"{lines}"
                f"shell.executable({json.dumps(step)})\n"
                f"shell.prefix(\"set -euo pipefail; \")\n")

    def command(self, ws) -> List[str]:
        return ["snakemake", "--cores", "4",
                "-s", os.path.join(ws.path, "Snakefile"),
                "-d", ws.path]

    def before_run(self, ws) -> None:
        """Rootful only: give the steps' group what the agent placed in the run.

        The steps run as another user, so the workspace and everything in it go to
        group STEP_GID, with the group's permissions copied from the owner's, and
        the directories set-group-ID so that what snakemake and the steps create
        stays in that group. Nothing outside the workspace changes.
        """
        if self.mode != "rootful":
            return
        for top, dirs, files in os.walk(ws.path):
            os.chown(top, -1, self.step_gid, follow_symlinks=False)
            os.chmod(top, 0o2770)
            for name in files:
                path = os.path.join(top, name)
                if os.path.islink(path):
                    continue
                os.chown(path, -1, self.step_gid, follow_symlinks=False)
                mode = os.stat(path, follow_symlinks=False).st_mode & 0o700
                os.chmod(path, mode | (mode >> 3))

    def after_run(self, ws) -> None:
        if self.mode == "rootful":
            # Removes the run's containers and gives the workspace back to the
            # agent, whatever modes the steps left on what they wrote.
            try:
                subprocess.run(["sudo", "-n", self.helper, "finish", ws.path],
                               capture_output=True, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
            return
        label = f"label=biochef.run={os.path.basename(ws.path)}"
        try:
            found = subprocess.run(
                ["podman", "ps", "--all", "--quiet", "--filter", label],
                capture_output=True, text=True, timeout=30, check=False)
            names = found.stdout.split()
            if names:
                subprocess.run(["podman", "rm", "--force", *names],
                               capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            # A leftover container costs little, and the run has its result.
            pass


TES_ISOLATION = ("attested", "none")
TES_STEP_CONF = "/etc/biochef-agent/tes-step.conf"
"""tes-step-root's configuration, at a path fixed in that script, for the reason
PODMAN_STEP_CONF is: the agent is not trusted to point root's helper elsewhere."""


def _read_tes_conf(path, owner):
    """STAGING_ROOT, read with the rules tes-step-root applies."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"{path} is missing; the tes runner needs it, see the README")
    if not _owned_by(path, owner):
        raise ValueError(f"{path} and its directory must be owned by root and "
                         f"writable by no one else")
    root = None
    with open(path, encoding="utf-8") as stream:
        for line in stream.read().splitlines():
            if not line or line.startswith("#"):
                continue
            key, equals, value = line.partition("=")
            if not equals or key != "STAGING_ROOT":
                raise ValueError(f"{path}: not a known KEY=value line: {line!r}")
            if root is not None:
                raise ValueError(f"{path}: STAGING_ROOT appears twice")
            root = value
    if root is None or not _ROOT_SHAPE.match(root):
        raise ValueError(f"{path}: STAGING_ROOT is missing or not valid")
    return root


class TesRunner(Runner):
    """Every step as a GA4GH TES task, sent to a server that already exists.

    Snakemake has no TES mode, so the seam is the shell its rules run under, as
    it is for podman: the preamble makes tes-step that shell, and the step's
    command goes to the server from there. Snakemake stays on the host.

    What the agent keeps, it keeps itself (tes.py): the image is only ever given
    by digest, the task holds no environment and no credential, and what comes
    back is checked by SHA-256 against a manifest the task wrote before it is
    placed in the workspace. A server that runs as root returns files owned by
    root, and tes-step-root, called through one sudo rule, gives them back.

    What it cannot keep is the sandbox: whether a task has a network or sees more
    than its input is the server's configuration. BIOCHEF_TES_ISOLATION has no
    default, because it is the operator's statement and nobody else can make it.
    `attested` says the server gives every task no network and only its input,
    and the network and filesystem guarantees are then `attested`, which
    BIOCHEF_REQUIRE_NETWORK_ISOLATION does not accept. `none` says it does not.
    The environment guarantee is `enforced` either way, because the task is built
    here from the command and the image alone.
    """

    name = "tes"

    def __init__(self, url: str = None, image: str = None, auth: str = None,
                 token_file: str = None, ca: str = None, staging: str = None,
                 helper: str = None, isolation: str = None,
                 step_conf: str = TES_STEP_CONF, trusted_owner: int = 0,
                 user: str = None, password_file: str = None):
        def given(value, name):
            return os.getenv(name, "") if value is None else value

        self.settings = tes.check_settings({
            "BIOCHEF_TES_URL": given(url, "BIOCHEF_TES_URL"),
            "BIOCHEF_TES_IMAGE": given(image, "BIOCHEF_TES_IMAGE"),
            "BIOCHEF_TES_AUTH": given(auth, "BIOCHEF_TES_AUTH"),
            "BIOCHEF_TES_TOKEN_FILE": given(token_file, "BIOCHEF_TES_TOKEN_FILE"),
            "BIOCHEF_TES_CA": given(ca, "BIOCHEF_TES_CA"),
            "BIOCHEF_TES_STAGING": given(staging, "BIOCHEF_TES_STAGING"),
            "BIOCHEF_TES_HELPER": given(helper, "BIOCHEF_TES_HELPER"),
            "BIOCHEF_TES_USER": given(user, "BIOCHEF_TES_USER"),
            "BIOCHEF_TES_PASSWORD_FILE": given(password_file, "BIOCHEF_TES_PASSWORD_FILE"),
        })
        self.image = self.settings.image
        self.isolation = given(isolation, "BIOCHEF_TES_ISOLATION").strip()
        if self.isolation not in TES_ISOLATION:
            raise ValueError(
                f"BIOCHEF_TES_ISOLATION={self.isolation!r}: it must be attested or "
                f"none, and has no default. attested is the operator's statement "
                f"that the TES server gives every task no network and only its "
                f"input; the agent cannot check it. none says it does not"
            )
        if os.geteuid() == 0:
            raise ValueError(
                "the agent must not run as root under the tes runner: it reaches "
                "root only through tes-step-root, and sudo gives it nothing to "
                "hand back to when it already is root"
            )
        staging = self.settings.staging
        info = os.stat(staging, follow_symlinks=False) if os.path.lexists(staging) else None
        if (info is None or not stat.S_ISDIR(info.st_mode)
                or os.path.realpath(staging) != staging):
            raise ValueError(
                f"BIOCHEF_TES_STAGING={staging!r} must be an existing directory, "
                f"named by no symlink")
        if info.st_uid != os.getuid() or info.st_mode & 0o007:
            raise ValueError(
                f"BIOCHEF_TES_STAGING={staging!r} must belong to the agent's user "
                f"and not be accessible to others: it holds the workspaces the "
                f"server reads, and what it returns")
        helper = self.settings.helper
        if (os.path.islink(helper) or not os.path.isfile(helper)
                or not _owned_by(helper, trusted_owner)):
            raise ValueError(
                f"BIOCHEF_TES_HELPER={helper!r} must be a file owned by root, in a "
                f"directory owned by root, neither writable by anyone else, or the "
                f"agent could change what runs as root"
            )
        root = _read_tes_conf(step_conf, trusted_owner)
        if root != staging:
            raise ValueError(
                f"BIOCHEF_TES_STAGING={staging!r} is not STAGING_ROOT={root!r} of "
                f"{step_conf}: tes-step-root hands back only what is under that"
            )
        try:
            allowed = subprocess.run(["sudo", "-n", "-l", helper],
                                     capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"the tes runner needs sudo: {error}") from None
        if allowed.returncode != 0:
            raise ValueError(
                f"sudo does not let this user run {helper} without a password; "
                f"the tes runner needs exactly that rule, see the README"
            )
        try:
            info = tes.Client(self.settings).service_info()
        except tes.TesError as error:
            raise ValueError(f"BIOCHEF_TES_URL={self.settings.url!r}: the TES server "
                             f"did not answer as one: {error}") from None
        self.server = " ".join(str(info[key]) for key in ("name", "version")
                               if isinstance(info.get(key), (str, int, float))) or "?"

    def with_image(self, image: str) -> "TesRunner":
        other = copy.copy(self)
        other.settings = tes.Settings(**{**self.settings._asdict(),
                                         "image": tes.check_image(image)})
        other.image = other.settings.image
        return other

    @property
    def guarantees(self) -> dict:
        said = ATTESTED if self.isolation == "attested" else NONE
        return {"network": said, "filesystem": said, "environment": ENFORCED}

    def describe(self) -> str:
        return (f"{self.name} ({self.settings.url}, {self.server}, "
                f"{self.image.split('@sha256:')[0]}@sha256:{self.image[-64:][:12]})")

    def environment(self):
        """Snakemake's environment, with what tes-step is to be given.

        The task never gets any of it. tes-step builds the task from the command
        and the image alone, and the token stays in its file.
        """
        values = self.settings._asdict()
        names = dict(zip(tes.ATTRIBUTES, ("url", "image", "auth", "token_file", "ca",
                                          "staging", "helper", "user",
                                          "password_file")))
        return {**os.environ,
                **{name: values[field] or "" for name, field in names.items()}}

    def snakefile_preamble(self) -> str:
        step = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tes-step")
        return (f"shell.executable({json.dumps(step)})\n"
                f"shell.prefix(\"set -euo pipefail; \")\n")

    def command(self, ws) -> List[str]:
        return ["snakemake", "--cores", "4",
                "-s", os.path.join(ws.path, "Snakefile"),
                "-d", ws.path]

    def after_run(self, ws) -> None:
        """Cancel what a killed run left on the server, then clear its staging.

        The group kill reaches tes-step and not the server, so a task that was
        running goes on running until it is cancelled here.
        """
        try:
            name = tes.run_name(ws.path)
        except tes.TesError:
            return
        try:
            tes.cancel_run(self.settings, name)
        except tes.TesError:
            # A server that cannot be reached cannot be told, and the run has its
            # result. What it holds is its own to expire.
            pass
        tes.clean_run(self.settings, name)


PROVIDERS = {
    SubprocessRunner.name: SubprocessRunner,
    ApptainerRunner.name: ApptainerRunner,
    SingularityRunner.name: SingularityRunner,
    PodmanRunner.name: PodmanRunner,
    TesRunner.name: TesRunner,
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
    chosen = provider()
    network = chosen.guarantees["network"]
    if require_network_isolation() and network != ENFORCED:
        raise ValueError(
            f"BIOCHEF_REQUIRE_NETWORK_ISOLATION, on unless set to false, refuses "
            f"BIOCHEF_RUNNER={name}: the agent does not itself keep its steps off "
            f"the network (network isolation is {network}), and an attested "
            f"isolation does not count. Use a runner that isolates the network, or "
            f"set BIOCHEF_REQUIRE_NETWORK_ISOLATION=false, which is audited at every "
            f"start. See UPGRADING.md"
        )
    return chosen


def require_network_isolation(value: str = None) -> bool:
    """BIOCHEF_REQUIRE_NETWORK_ISOLATION: true, false, or unset for true.

    On by default: a step that can reach the network is the thing a TRE exists to
    prevent, so running one has to be asked for, with `false`, and the agent
    records that it was (main.record_isolation_choice).
    """
    raw = (os.getenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "") if value is None
           else value).strip()
    if raw in ("", "true"):
        return True
    if raw == "false":
        return False
    raise ValueError(f"BIOCHEF_REQUIRE_NETWORK_ISOLATION={raw!r} must be true or false")


_PROBE_SHAPE = re.compile(r"\A([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([1-9][0-9]{0,4})\Z")


def network_probe(value: str = None):
    """BIOCHEF_NETWORK_PROBE: host:port the agent can reach and a step must not."""
    raw = (os.getenv("BIOCHEF_NETWORK_PROBE", "") if value is None else value).strip()
    if not raw:
        return None
    found = _PROBE_SHAPE.match(raw)
    if not found or int(found.group(2)) > 65535:
        raise ValueError(f"BIOCHEF_NETWORK_PROBE={raw!r} must be host:port")
    return found.group(1), int(found.group(2))


class NetworkCheck(NamedTuple):
    ok: bool
    detail: str


NET = "/proc/net"
"""Where a step reads its routes. A constant so that the tests can run the real
rule against files of their own."""

_CHECK_RULE = r'''
rule network_check:
    output: "network-check.txt"
    shell:
        "up=0; read=0; if [ -r __NET__/route ]; then read=1; "
        "while read -r dev rest; do [ \"$dev\" = Iface ] || [ \"$dev\" = lo ] "
        "|| up=$((up+1)); done < __NET__/route; fi; "
        "if [ -r __NET__/if_inet6 ]; then while read -r a b c d e dev; do "
        "[ \"$dev\" = lo ] || up=$((up+1)); done < __NET__/if_inet6; fi; "
        "echo $up $read > {output}; "
        __REACH__
'''

_REACH = ('"if timeout 5 bash -c \'exec 3<>/dev/tcp/{host}/{port}\' 2>/dev/null; "\n'
          '        "then echo reachable; else echo unreachable; fi >> {{output}}"')


def check_network(provider: Runner, root: str = None, timeout_s: int = 600,
                  probe=None, force: bool = False):
    """One step that tries the network, run the way every step is run.

    For a runner whose network guarantee is enforced, at start, so that a host
    where the flag is accepted and has no effect is found before any data is.
    The step counts the IPv4 routes and IPv6 addresses on anything but loopback,
    which must be none. It reads them from /proc/net, which is the step's own
    network namespace, and not from /sys/class/net, which apptainer binds from
    the host, so it lists the host's interfaces whatever the namespace. With a probe, it also tries to connect to it, which must fail, and the
    agent first connects to it itself, so that "unreachable" means isolation and
    not a network that is down. Returns None for a runner that claims nothing,
    unless `force`.
    """
    if provider.guarantees["network"] != ENFORCED and not force:
        return None
    probe = network_probe() if probe is None else probe
    if probe:
        try:
            socket.create_connection(probe, timeout=5).close()
        except OSError as error:
            return NetworkCheck(False, f"the agent itself cannot reach "
                                       f"{probe[0]}:{probe[1]} ({error}), so a step "
                                       f"that cannot either proves nothing")
        reach = _REACH.format(host=probe[0], port=probe[1])
    else:
        reach = '"true"'
    path = tempfile.mkdtemp(prefix="biochef-run-", dir=root or None)

    class _Workspace:
        pass

    ws = _Workspace()
    ws.path = path
    try:
        with open(os.path.join(path, "Snakefile"), "w") as stream:
            stream.write(provider.snakefile_preamble()
                         + _CHECK_RULE.replace("__REACH__", reach)
                         .replace("__NET__", NET))
        result = provider.run(ws, timeout_s=timeout_s)
        if result.returncode != 0:
            return NetworkCheck(False, f"the check step did not run (exit "
                                       f"{result.returncode}): {result.stderr[-500:]}")
        with open(os.path.join(path, "network-check.txt")) as stream:
            seen = stream.read().split()
    finally:
        shutil.rmtree(path, ignore_errors=True)
    found, readable = (seen + ["?", "?"])[:2]
    if readable != "1":
        # The step could not read its routes at all, so "none" would mean nothing.
        return NetworkCheck(False, "a step could not read its network routes")
    if found != "0":
        return NetworkCheck(False, f"a step saw {found} routes or addresses besides "
                                   f"loopback")
    if probe and seen[2:] != ["unreachable"]:
        return NetworkCheck(False, f"a step reached {probe[0]}:{probe[1]}")
    if probe:
        return NetworkCheck(True, f"no route or address but loopback, and "
                                  f"{probe[0]}:{probe[1]} unreachable from a step")
    return NetworkCheck(True, "no route or address but loopback (no "
                              "BIOCHEF_NETWORK_PROBE to try a connection)")
