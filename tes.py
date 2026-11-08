"""A step as a GA4GH TES task, sent to a server that already exists.

SPEC.md, section 14.2. The podman and apptainer runners start a container
themselves. This one hands the step to a Task Execution Service, such as Funnel,
which a TRE may already run and which then decides where the step runs. What the
agent keeps is everything it can check for itself:

  the image    only ever `repo@sha256:...`, so the server cannot be given a tag
               that moves, and the server's answer is not needed to know what ran
  the task     built here from scratch, from the command and the image alone: no
               environment, no credential, nothing of the agent's. The server's
               token travels in a header, read from a file at each request
  the files    the step's workspace is copied into a directory of its own under
               BIOCHEF_TES_STAGING, which is all the server is ever shown. What
               comes back is checked, by SHA-256, against a manifest the step's own
               task wrote, and only then placed in the workspace
  the owner    a server running as root returns files owned by root. A helper
               called through one sudo rule (tes-step-root) gives them back to the
               agent, as podman-step-root does for rootful podman

What it cannot keep is the sandbox itself: whether a task has a network, or sees
more of the host than its input, is the server's configuration. The runner says so
(`attested`, never `enforced`), and BIOCHEF_REQUIRE_NETWORK_ISOLATION refuses it
unless that rule is turned off on purpose.

This is a module, and tes-step a few lines around `main`, because Snakemake runs
the script as a shell and a script cannot be tested the way a function can. Only
the standard library, so that the script runs under whatever python3 the host has.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, NamedTuple, Optional

WORKDIR = "/work"
"""Where the task sees the step's workspace, writable and its own copy."""
OUTDIR = "/out"
"""Where the task puts what the step changed, and the manifest of it."""
MANIFEST = ".biochef-manifest"

TERMINAL = frozenset({"COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED",
                      "CANCELLED", "PREEMPTED"})
"""TES states after which a task changes no more. Both spellings of canceled."""

NOT_STAGED = frozenset({".snakemake", "Snakefile", ".tes"})
"""Names at the top of a workspace that a step has no use for: Snakemake's own
state, and the workflow. A task cannot hand them back either."""

ATTRIBUTES = ("BIOCHEF_TES_URL", "BIOCHEF_TES_IMAGE", "BIOCHEF_TES_AUTH",
              "BIOCHEF_TES_TOKEN_FILE", "BIOCHEF_TES_CA", "BIOCHEF_TES_STAGING",
              "BIOCHEF_TES_HELPER")
"""What tes-step is given, by the runner, in Snakemake's environment."""

_IMAGE_SHAPE = re.compile(r"\A(?!.*//)[A-Za-z0-9][A-Za-z0-9._:/-]{0,250}@sha256:[0-9a-f]{64}\Z")
_RUN_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TOKEN_SHAPE = re.compile(r"\A[A-Za-z0-9._~+/=-]{8,4096}\Z")
_MANIFEST_LINE = re.compile(r"\A([0-9a-f]{64})  \./([^\x00-\x1f\x7f\\]+)\Z")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
MAX_RESPONSE = 16 * 1024 * 1024


class TesError(ValueError):
    """Something the agent refuses to go on with, said so an operator can act on it."""


class Settings(NamedTuple):
    url: str
    image: str
    auth: str
    token_file: Optional[str]
    ca: Optional[str]
    staging: str
    helper: str


def check_url(url: str) -> str:
    """https, or http to this very host; no credential in it; no query."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise TesError(f"BIOCHEF_TES_URL={url!r} is not a URL")
    parts = urllib.parse.urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise TesError(f"BIOCHEF_TES_URL={url!r} has a bad port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TesError(f"BIOCHEF_TES_URL={url!r} must be an http or https URL")
    if parts.username is not None or parts.password is not None:
        raise TesError("BIOCHEF_TES_URL must not carry a user or password: the "
                       "token is BIOCHEF_TES_TOKEN_FILE, sent in a header")
    if parts.query or parts.fragment:
        raise TesError(f"BIOCHEF_TES_URL={url!r} must have no query or fragment")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise TesError(f"BIOCHEF_TES_URL={url!r} is plain http to another host: "
                       f"a task and the token would cross the network in the "
                       f"clear. Use https, or http only to this host")
    return url.rstrip("/")


def check_image(image: str) -> str:
    """repo@sha256:<64 hex>, with no scheme and no tag that could move."""
    if not _IMAGE_SHAPE.match(image or ""):
        raise TesError(
            f"BIOCHEF_TES_IMAGE={image!r} must be an image by digest, "
            f"repository@sha256:<64 hex digits>, with no scheme. A tag can point "
            f"to another image tomorrow, and the task would run it")
    return image


def check_settings(values) -> Settings:
    """Everything tes-step needs, from a mapping of the BIOCHEF_TES_* names."""
    def get(name):
        return (values.get(name) or "").strip()

    auth = get("BIOCHEF_TES_AUTH")
    if auth not in ("bearer", "none"):
        raise TesError(f"BIOCHEF_TES_AUTH={auth!r} must be bearer or none, and has "
                       f"no default")
    token_file = get("BIOCHEF_TES_TOKEN_FILE") or None
    if auth == "bearer":
        if not token_file or not os.path.isabs(token_file):
            raise TesError("BIOCHEF_TES_AUTH=bearer needs BIOCHEF_TES_TOKEN_FILE, "
                           "the absolute path of a file holding the token")
        read_token(token_file)
    elif token_file:
        raise TesError("BIOCHEF_TES_TOKEN_FILE is set but BIOCHEF_TES_AUTH=none, "
                       "so the token would never be sent")
    ca = get("BIOCHEF_TES_CA") or None
    if ca and not (os.path.isabs(ca) and os.path.isfile(ca)):
        raise TesError(f"BIOCHEF_TES_CA={ca!r} must be the absolute path of a file")
    staging = get("BIOCHEF_TES_STAGING")
    if not os.path.isabs(staging):
        raise TesError(f"BIOCHEF_TES_STAGING={staging!r} must be an absolute path "
                       f"the server can read, and has no default")
    helper = get("BIOCHEF_TES_HELPER")
    if not os.path.isabs(helper):
        raise TesError(f"BIOCHEF_TES_HELPER={helper!r} must be the absolute path of "
                       f"tes-step-root as installed for sudo, and has no default")
    return Settings(check_url(get("BIOCHEF_TES_URL")),
                    check_image(get("BIOCHEF_TES_IMAGE")), auth, token_file, ca,
                    staging, helper)


def read_token(path: str) -> str:
    """The token, from a file only the agent's user can read."""
    try:
        info = os.lstat(path)
    except OSError as error:
        raise TesError(f"BIOCHEF_TES_TOKEN_FILE={path!r}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise TesError(f"BIOCHEF_TES_TOKEN_FILE={path!r} is not a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TesError(f"BIOCHEF_TES_TOKEN_FILE={path!r} must belong to the agent's "
                       f"user and be readable by no one else (mode 0600)")
    with open(path, encoding="utf-8") as stream:
        token = stream.read(8192).strip()
    if not _TOKEN_SHAPE.match(token):
        raise TesError(f"BIOCHEF_TES_TOKEN_FILE={path!r} does not hold a token")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would send the token wherever the server says."""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """The few TES calls the agent makes, over HTTP, with the token in a header."""

    def __init__(self, settings: Settings, timeout: float = 30):
        self.settings = settings
        self.timeout = timeout
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if settings.url.startswith("https://"):
            context = ssl.create_default_context(cafile=settings.ca)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def _call(self, method, path, body=None, query=None):
        url = self.settings.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.settings.auth == "bearer":
            headers["Authorization"] = "Bearer " + read_token(self.settings.token_file)
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            detail = error.read(300).decode("utf-8", "replace")
            raise TesError(f"{method} {path}: the server answered "
                           f"{error.code} {detail}".strip()) from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as error:
            raise TesError(f"{method} {path}: {getattr(error, 'reason', error)}") from None
        if len(raw) > MAX_RESPONSE:
            raise TesError(f"{method} {path}: the answer is larger than "
                           f"{MAX_RESPONSE} bytes")
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise TesError(f"{method} {path}: the answer is not JSON") from None

    def service_info(self) -> dict:
        info = self._call("GET", "/v1/service-info")
        if not isinstance(info, dict):
            raise TesError("service-info is not an object")
        kind = (info.get("type") or {}).get("artifact")
        if kind is not None and kind != "tes":
            raise TesError(f"{self.settings.url} is a {kind!r} service, not a TES one")
        return info

    def submit(self, task: dict) -> str:
        answer = self._call("POST", "/v1/tasks", task)
        task_id = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(task_id, str) or not re.match(r"\A[A-Za-z0-9._-]{1,200}\Z",
                                                        task_id):
            raise TesError("the server accepted the task and gave no usable id")
        return task_id

    def get(self, task_id: str) -> dict:
        task = self._call("GET", f"/v1/tasks/{urllib.parse.quote(task_id)}",
                          query={"view": "FULL"})
        if not isinstance(task, dict):
            raise TesError("a task is not an object")
        return task

    def cancel(self, task_id: str) -> None:
        self._call("POST", f"/v1/tasks/{urllib.parse.quote(task_id)}:cancel")

    def active(self, name_prefix: str) -> List[str]:
        """The ids of this run's tasks that have not finished."""
        found, token = [], None
        for _ in range(50):
            query = {"view": "MINIMAL", "page_size": 100, "name_prefix": name_prefix}
            if token:
                query["page_token"] = token
            page = self._call("GET", "/v1/tasks", query=query)
            for task in page.get("tasks") or []:
                if task.get("state") not in TERMINAL and isinstance(task.get("id"), str):
                    found.append(task["id"])
            token = page.get("next_page_token")
            if not token:
                break
        return found

    def wait(self, task_id: str, sleep=time.sleep) -> dict:
        """Poll until the task has finished. A few failed polls are not an end."""
        delay, failures = 0.5, 0
        while True:
            try:
                task = self.get(task_id)
                failures = 0
            except TesError:
                failures += 1
                if failures >= 4:
                    raise
                task = {}
            if task.get("state") in TERMINAL:
                return task
            sleep(delay)
            delay = min(delay * 1.5, 5)


def run_name(workspace: str) -> str:
    name = os.path.basename(os.path.realpath(workspace))
    if not _RUN_NAME.match(name):
        raise TesError(f"the workspace name {name!r} is not one tes-step can use")
    return name


def task_prefix(name: str) -> str:
    return f"biochef-{name}-"


# What runs inside the task. $1 is the step's command, whole, so nothing of it is
# ever spliced into this text. It hashes the workspace before and after, copies
# only what the step created or changed to /out, and writes a manifest of exactly
# what is there. The manifest is what the agent checks the arrival against.
# find -exec sha256sum, sort and comm are in busybox as well as GNU coreutils.
WRAPPER = r'''set -euo pipefail
export LC_ALL=C
cd /work
find . -type f -exec sha256sum {} + | sort > /tmp/before
rc=0
bash -c "$1" || rc=$?
if [ "$rc" -ne 0 ]; then exit "$rc"; fi
find . -type f -exec sha256sum {} + | sort > /tmp/after
mkdir -p /out
comm -13 /tmp/before /tmp/after | while IFS= read -r line; do
    p=${line#* }
    p=${p# }
    mkdir -p "/out/$(dirname "$p")"
    cp "$p" "/out/$p"
done
(cd /out && find . -type f -exec sha256sum {} +) > /tmp/manifest
mv /tmp/manifest /out/.biochef-manifest
'''


def build_task(image: str, command: str, name: str, step: str, in_url: str,
               out_url: str) -> dict:
    """The task, from the command, the image and two URLs. Nothing else goes in."""
    return {
        "name": f"{task_prefix(name)}{step}",
        "description": "one step of a biochef-agent run",
        "tags": {"biochef.run": name},
        "executors": [{
            "image": image,
            "command": ["bash", "-c", WRAPPER, "biochef-step", command],
            "workdir": WORKDIR,
        }],
        "inputs": [{"url": in_url, "path": WORKDIR, "type": "DIRECTORY"}],
        "outputs": [{"url": out_url, "path": OUTDIR, "type": "DIRECTORY"}],
    }


def _file_url(path: str) -> str:
    return "file://" + urllib.parse.quote(path)


def stage_inputs(workspace: str, destination: str) -> None:
    """Copy the workspace to where the server will read it, refusing anything odd.

    A symbolic link would be followed by a server running as root, so a step could
    be handed any file on the host, and a special file would be worse. Both stop
    the step here.
    """
    os.mkdir(destination, 0o770)
    os.chmod(destination, 0o770)
    for top, dirs, files in os.walk(workspace):
        relative = os.path.relpath(top, workspace)
        if relative == ".":
            dirs[:] = [name for name in dirs if name not in NOT_STAGED]
            files[:] = [name for name in files if name not in NOT_STAGED]
        target = destination if relative == "." else os.path.join(destination, relative)
        for name in list(dirs):
            info = os.lstat(os.path.join(top, name))
            if not stat.S_ISDIR(info.st_mode):
                raise TesError(f"{os.path.join(relative, name)} in the workspace is a "
                               f"link, and would be followed by the server")
            os.mkdir(os.path.join(target, name), 0o770)
            os.chmod(os.path.join(target, name), 0o770)
        for name in files:
            source = os.path.join(top, name)
            info = os.lstat(source)
            if not stat.S_ISREG(info.st_mode):
                raise TesError(f"{os.path.normpath(os.path.join(relative, name))} in "
                               f"the workspace is not a regular file")
            copied = os.path.join(target, name)
            shutil.copyfile(source, copied, follow_symlinks=False)
            os.chmod(copied, 0o770 if info.st_mode & 0o100 else 0o660)


def _sha256(path: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_arrival(out: str) -> List[str]:
    """What arrived, checked against the manifest. Returns the files, or refuses.

    Everything must be a plain file or directory, every file must be in the
    manifest with the digest listed there, and nothing the manifest lists may be
    missing. A manifest that is itself missing is a refusal: without it nothing
    says what was meant to arrive.
    """
    manifest_path = os.path.join(out, MANIFEST)
    try:
        if not stat.S_ISREG(os.lstat(manifest_path).st_mode):
            raise TesError("the manifest is not a regular file")
        with open(manifest_path, encoding="utf-8") as stream:
            lines = stream.read(64 * 1024 * 1024).splitlines()
    except (OSError, UnicodeDecodeError):
        raise TesError("the task returned no readable manifest of its outputs") from None
    expected = {}
    for line in lines:
        found = _MANIFEST_LINE.match(line)
        if not found:
            raise TesError(f"a line of the manifest is not a digest and a plain "
                           f"path: {line[:80]!r}")
        path = found.group(2)
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise TesError(f"the manifest names {path!r}, which leaves the workspace")
        if path in expected:
            raise TesError(f"the manifest lists {path!r} twice")
        expected[path] = found.group(1)
    arrived = {}
    for top, dirs, files in os.walk(out):
        for name in dirs:
            if not stat.S_ISDIR(os.lstat(os.path.join(top, name)).st_mode):
                raise TesError(f"{name} arrived as a link, not a directory")
        for name in files:
            full = os.path.join(top, name)
            info = os.lstat(full)
            relative = os.path.relpath(full, out)
            if not stat.S_ISREG(info.st_mode):
                raise TesError(f"{relative} arrived as something other than a file")
            if relative != MANIFEST:
                arrived[relative] = full
    unlisted = sorted(set(arrived) - set(expected))
    if unlisted:
        raise TesError(f"{unlisted[0]} arrived and is not in the manifest")
    missing = sorted(set(expected) - set(arrived))
    if missing:
        raise TesError(f"{missing[0]} is in the manifest and did not arrive")
    for relative in sorted(arrived):
        if _sha256(arrived[relative]) != expected[relative]:
            raise TesError(f"{relative} arrived with a different SHA-256 from the "
                           f"one the task recorded")
    return sorted(arrived)


def apply_arrival(out: str, workspace: str, files: List[str]) -> None:
    """Place verified files in the workspace, in no place a step may not write."""
    for relative in files:
        parts = relative.split("/")
        if parts[0] in NOT_STAGED or parts[0].startswith(".snakemake"):
            raise TesError(f"the task returned {relative!r}, which belongs to the "
                           f"agent and not to a step")
    for relative in files:
        parts = relative.split("/")
        directory = workspace
        for part in parts[:-1]:
            directory = os.path.join(directory, part)
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise TesError(f"{relative!r} would be written through a link")
        destination = os.path.join(workspace, relative)
        if os.path.lexists(destination) and not stat.S_ISREG(
                os.lstat(destination).st_mode):
            raise TesError(f"{relative!r} would replace something that is not a file")
        source = os.path.join(out, relative)
        executable = os.lstat(source).st_mode & 0o100
        handle, partial = tempfile.mkstemp(prefix=".tes-", dir=directory)
        try:
            with os.fdopen(handle, "wb") as stream, open(source, "rb") as origin:
                shutil.copyfileobj(origin, stream)
            os.chmod(partial, 0o700 if executable else 0o600)
            os.replace(partial, destination)
        except BaseException:
            try:
                os.unlink(partial)
            except OSError:
                pass
            raise


def give_back(settings: Settings, directory: str) -> None:
    """Have the helper hand a step's directory back to the agent's user."""
    try:
        done = subprocess.run(["sudo", "-n", "--", settings.helper, "finish", directory],
                              capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise TesError(f"could not run {settings.helper}: {error}") from None
    if done.returncode != 0:
        raise TesError(f"{settings.helper} finish {directory} failed "
                       f"({done.returncode}): {done.stderr.strip()[:300]}")


def clean_run(settings: Settings, name: str) -> None:
    """Remove what a run left in staging, handing it back first."""
    run_directory = os.path.join(settings.staging, name)
    if not os.path.isdir(run_directory) or os.path.islink(run_directory):
        return
    for entry in sorted(os.listdir(run_directory)):
        step = os.path.join(run_directory, entry)
        if os.path.isdir(step) and not os.path.islink(step):
            try:
                give_back(settings, step)
            except TesError:
                pass
    shutil.rmtree(run_directory, ignore_errors=True)


def cancel_run(settings: Settings, name: str, client: Client = None,
               sleep=time.sleep) -> int:
    """Cancel the run's tasks that are still going, and wait for them to stop."""
    client = client or Client(settings)
    prefix = task_prefix(name)
    cancelled = 0
    for task_id in client.active(prefix):
        client.cancel(task_id)
        cancelled += 1
    for _ in range(60):
        if not client.active(prefix):
            break
        sleep(0.5)
    return cancelled


def _print_logs(task: dict, out=sys.stdout, err=sys.stderr) -> Optional[int]:
    """Show what the executor wrote. Returns its exit code if it gave one."""
    code = None
    for attempt in task.get("logs") or []:
        for executor in attempt.get("logs") or []:
            out.write(executor.get("stdout") or "")
            err.write(executor.get("stderr") or "")
            if isinstance(executor.get("exit_code"), int):
                code = executor["exit_code"]
        for line in attempt.get("system_logs") or []:
            err.write(f"tes-step: server: {line}\n")
    out.flush()
    err.flush()
    return code


def main(argv: List[str], environ=None, client: Client = None, out=sys.stdout,
         err=sys.stderr) -> int:
    """Run one step as a task: stage, submit, wait, check, place, clean."""
    environ = os.environ if environ is None else environ
    if len(argv) != 2 or argv[0] != "-c":
        err.write("tes-step: expects: -c <command>\n")
        return 2
    command = argv[1]
    try:
        settings = check_settings(environ)
        workspace = os.path.realpath(os.getcwd())
        name = run_name(workspace)
        client = client or Client(settings)
    except TesError as error:
        err.write(f"tes-step: {error}\n")
        return 2

    run_directory = os.path.join(settings.staging, name)
    step_directory = None
    task_id = None
    finished = False
    code = 1

    def interrupted(signum, frame):
        raise KeyboardInterrupt(signum)

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        try:
            os.mkdir(run_directory, 0o770)
        except FileExistsError:
            pass
        if os.path.islink(run_directory) or not os.path.isdir(run_directory):
            raise TesError(f"{run_directory} is not a directory of the agent's")
        os.chmod(run_directory, 0o770)
        step_directory = tempfile.mkdtemp(prefix="s", dir=run_directory)
        os.chmod(step_directory, 0o770)
        stage_inputs(workspace, os.path.join(step_directory, "in"))
        task = build_task(
            settings.image, command, name, os.path.basename(step_directory),
            _file_url(os.path.join(step_directory, "in")),
            _file_url(os.path.join(step_directory, "out")))
        task_id = client.submit(task)
        final = client.wait(task_id)
        finished = True
        reported = _print_logs(final, out, err)
        state = final.get("state")
        if state == "COMPLETE":
            give_back(settings, step_directory)
            files = verify_arrival(os.path.join(step_directory, "out"))
            apply_arrival(os.path.join(step_directory, "out"), workspace, files)
            code = 0
        elif state == "EXECUTOR_ERROR":
            code = reported if isinstance(reported, int) and reported != 0 else 1
        else:
            err.write(f"tes-step: the task ended {state}\n")
            code = 1
    except TesError as error:
        err.write(f"tes-step: {error}\n")
        code = 2 if task_id is None else 1
    except OSError as error:
        err.write(f"tes-step: {error}\n")
        code = 2 if task_id is None else 1
    except KeyboardInterrupt:
        err.write("tes-step: interrupted, cancelling the task\n")
        code = 143
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if task_id is not None and not finished:
            try:
                client.cancel(task_id)
            except TesError:
                pass
        if step_directory is not None:
            try:
                give_back(settings, step_directory)
            except TesError:
                pass
            shutil.rmtree(step_directory, ignore_errors=True)
    return code
