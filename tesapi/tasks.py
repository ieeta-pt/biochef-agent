"""A TES task: what is accepted, how it runs, and what it leaves behind.

A task becomes a Snakefile of one rule and runs through the same runner as a
federated step, so it gets that runner's network, filesystem and environment
guarantees and nothing weaker. It runs in a workspace of its own, made from its
inputs; its outputs are copied out of that workspace to where it asked, and the
workspace is removed. It is never given a path of the node's datasets, and never
runs with a setting of the agent's or a credential of the task's own: a task has
no `env`.

Accepted, narrower than TES: one executor; `command` is an argument list;
`workdir` is /work or absent; no env, stdin, stdout, stderr, volumes or resources,
because each would either put something of the caller's into the step or ask for
what this node does not promise. Anything not understood is refused rather than
ignored.
"""

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

from starlette.concurrency import run_in_threadpool

import audit
from tesapi.config import MAX_KEPT_TASKS
from tesapi.policy import WORKDIR, Policy, Refused
from workspace import make_workspace

SOURCE = "tes"
"""The `source` of every audit event written here."""

TERMINAL = ("COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED")
MAX_INLINE = 1024 * 1024
MAX_LOG = 64 * 1024
_TOP = {"name", "description", "tags", "executors", "inputs", "outputs"}
_REFUSED_TOP = {"resources": "resource requests are not honoured by this node",
                "volumes": "volumes would mount parts of the host"}
_EXECUTOR = {"image", "command", "workdir"}
_INPUT = {"url", "content", "path", "type", "name", "description"}
_OUTPUT = {"url", "path", "type", "name", "description"}


class Item(NamedTuple):
    kind: str
    relative: str
    location: Optional[str]
    content: Optional[str]
    url: Optional[str]


class Plan(NamedTuple):
    name: str
    description: str
    tags: dict
    image: str
    command: List[str]
    inputs: List[Item]
    outputs: List[Item]
    submitted: dict


def _text(value, field, limit, empty=True):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise Refused(f"{field} must be text of at most {limit} characters")
    return value


def _kind(value, field):
    if value in (None, "", "FILE"):
        return "FILE"
    if value == "DIRECTORY":
        return "DIRECTORY"
    raise Refused(f"{field}.type must be FILE or DIRECTORY")


def parse(body, policy: Policy) -> Plan:
    """A task body to a plan, or a refusal that says why."""
    if not isinstance(body, dict):
        raise Refused("a task is a JSON object")
    for key, reason in _REFUSED_TOP.items():
        if body.get(key):
            raise Refused(f"{key}: {reason}")
    unknown = sorted(set(body) - _TOP - set(_REFUSED_TOP))
    if unknown:
        raise Refused(f"not understood, and so refused: {', '.join(unknown)}")
    name = _text(body.get("name"), "name", 128)
    description = _text(body.get("description"), "description", 1000)
    tags = body.get("tags") or {}
    if (not isinstance(tags, dict) or len(tags) > 16
            or any(not isinstance(k, str) or not isinstance(v, str)
                   or len(k) > 128 or len(v) > 256 for k, v in tags.items())):
        raise Refused("tags are at most 16 pairs of short text")

    executors = body.get("executors")
    if not isinstance(executors, list) or len(executors) != 1:
        raise Refused("a task has exactly one executor on this node")
    executor = executors[0]
    if not isinstance(executor, dict):
        raise Refused("an executor is an object")
    unknown = sorted(set(executor) - _EXECUTOR)
    if unknown:
        raise Refused(f"executor: not understood, and so refused: {', '.join(unknown)}"
                      f" (env, stdin, stdout, stderr and ignore_error are not accepted)")
    image = policy.image(executor.get("image"))
    command = executor.get("command")
    if (not isinstance(command, list) or not 1 <= len(command) <= 256
            or any(not isinstance(a, str) or len(a) > 8192 or "\x00" in a
                   for a in command)
            or sum(len(a) for a in command) > 65536):
        raise Refused("command is a list of 1 to 256 arguments of text")
    if executor.get("workdir") not in (None, "", WORKDIR):
        raise Refused(f"workdir must be {WORKDIR} or absent")

    inputs = []
    for number, item in enumerate(_list(body.get("inputs"), "inputs"), 1):
        field = f"inputs[{number}]"
        _members(item, _INPUT, field)
        kind = _kind(item.get("type"), field)
        relative = policy.workspace_path(item.get("path"), kind == "DIRECTORY")
        if (item.get("url") is None) == (item.get("content") is None):
            raise Refused(f"{field} needs exactly one of url and content")
        if item.get("content") is not None:
            content = item["content"]
            if (kind != "FILE" or not isinstance(content, str)
                    or len(content.encode()) > MAX_INLINE):
                raise Refused(f"{field}.content is text of at most {MAX_INLINE} bytes, "
                              f"for a FILE")
            inputs.append(Item(kind, relative, None, content, None))
        else:
            location = policy.source(item["url"])
            is_directory = os.path.isdir(location)
            if (kind == "DIRECTORY") != is_directory:
                raise Refused(f"{field}: {location} is "
                              f"{'a directory' if is_directory else 'a file'}, "
                              f"not the {kind} the task says")
            inputs.append(Item(kind, relative, location, None, item["url"]))
    outputs = []
    for number, item in enumerate(_list(body.get("outputs"), "outputs"), 1):
        field = f"outputs[{number}]"
        _members(item, _OUTPUT, field)
        kind = _kind(item.get("type"), field)
        relative = policy.workspace_path(item.get("path"))
        outputs.append(Item(kind, relative, policy.destination(item.get("url")), None,
                            item["url"]))
    for kind, items in (("inputs", inputs), ("outputs", outputs)):
        seen = []
        for item in items:
            for other in seen:
                if (item.relative == other or item.relative.startswith(other + "/")
                        or other.startswith(item.relative + "/")):
                    raise Refused(f"two {kind} overlap at /work/{item.relative}")
            seen.append(item.relative)
    return Plan(name, description, tags, image, command, inputs, outputs, body)


def _list(value, field):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise Refused(f"{field} is a list of at most 64")
    return value


def _members(item, allowed, field):
    if not isinstance(item, dict):
        raise Refused(f"{field} is an object")
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise Refused(f"{field}: not understood, and so refused: {', '.join(unknown)}")


def snakefile(preamble: str, plan: Plan, workspace: str) -> str:
    """One rule that runs the command, in the workspace, then marks itself done.

    Arguments that start with /work name the workspace, which is where the command
    runs, so they are written as its path. The text is a Python literal, and its
    braces are doubled because Snakemake formats a shell string.
    """
    def local(argument):
        if argument == WORKDIR or argument.startswith(WORKDIR + "/"):
            return workspace + argument[len(WORKDIR):]
        return argument

    text = shlex.join(local(a) for a in plan.command) + "\ntouch .biochef-done\n"
    literal = json.dumps(text.replace("{", "{{").replace("}", "}}"))
    return (f"{preamble}\nrule all:\n    input: \".biochef-done\"\n\n"
            f"rule task:\n    output: \".biochef-done\"\n    shell: {literal}\n")


# --------------------------------------------------------------------------
# files in and out of the workspace


class TooBig(Exception):
    pass


class Budget:
    def __init__(self, limit):
        self.left = limit

    def spend(self, size):
        self.left -= size
        if self.left < 0:
            raise TooBig()


def _copy_file(source, destination, budget, mode=0o600):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as origin:
        info = os.fstat(origin.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{source} is not a regular file", 403)
        budget.spend(info.st_size)
        handle, partial = tempfile.mkstemp(prefix=".tes-", dir=os.path.dirname(destination))
        try:
            with os.fdopen(handle, "wb") as target:
                shutil.copyfileobj(origin, target)
            os.chmod(partial, mode | (0o100 if info.st_mode & 0o100 else 0))
            os.replace(partial, destination)
        except BaseException:
            try:
                os.unlink(partial)
            except OSError:
                pass
            raise


def _copy_tree(source, destination, budget, replace):
    os.makedirs(destination, mode=0o750, exist_ok=True)
    if os.path.islink(destination) or not os.path.isdir(destination):
        raise Refused(f"{destination} is not a directory", 403)
    for top, dirs, files in os.walk(source):
        relative = os.path.relpath(top, source)
        target = destination if relative == "." else os.path.join(destination, relative)
        for name in dirs:
            if not stat.S_ISDIR(os.lstat(os.path.join(top, name)).st_mode):
                raise Refused(f"{os.path.join(top, name)} is a link", 403)
            path = os.path.join(target, name)
            os.makedirs(path, mode=0o750, exist_ok=True)
            if os.path.islink(path):
                raise Refused(f"{path} is a link", 403)
        for name in files:
            origin, path = os.path.join(top, name), os.path.join(target, name)
            if not stat.S_ISREG(os.lstat(origin).st_mode):
                raise Refused(f"{origin} is not a plain file", 403)
            if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
                raise Refused(f"{path} is not a plain file", 403)
            _copy_file(origin, path, budget)


def stage_in(plan: Plan, workspace: str, budget: Budget):
    for item in plan.inputs:
        target = os.path.join(workspace, item.relative) if item.relative else workspace
        parent = os.path.dirname(target)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if item.content is not None:
            data = item.content.encode()
            budget.spend(len(data))
            with open(target, "xb") as stream:
                stream.write(data)
            os.chmod(target, 0o600)
        elif item.kind == "DIRECTORY":
            _copy_tree(item.location, target, budget, replace=False)
        else:
            _copy_file(item.location, target, budget)


def stage_out(plan: Plan, workspace: str, budget: Budget, policy: Policy):
    """Copy outputs to where the task asked, checking again that it is allowed."""
    sizes = []
    for item in plan.outputs:
        source = os.path.join(workspace, item.relative)
        info = os.lstat(source) if os.path.lexists(source) else None
        if info is None:
            raise Refused(f"the task did not produce /work/{item.relative}", 500)
        destination = policy.destination(item.url)
        if item.kind == "DIRECTORY":
            if not stat.S_ISDIR(info.st_mode):
                raise Refused(f"/work/{item.relative} is not a directory", 500)
            _copy_tree(source, destination, budget, replace=True)
            sizes.append(None)
        else:
            if not stat.S_ISREG(info.st_mode):
                raise Refused(f"/work/{item.relative} is not a regular file", 500)
            if os.path.lexists(destination) and not stat.S_ISREG(
                    os.lstat(destination).st_mode):
                raise Refused(f"{destination} is not a plain file", 403)
            _copy_file(source, destination, budget, mode=0o640)
            sizes.append(info.st_size)
    return sizes


# --------------------------------------------------------------------------
# the tasks the listener knows


def now():
    return datetime.now(timezone.utc).isoformat()


class Task:
    def __init__(self, owner: str, plan: Plan):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.plan = plan
        self.state = "QUEUED"
        self.created = now()
        self.started = self.ended = None
        self.stdout = self.stderr = ""
        self.exit_code = None
        self.system_logs: List[str] = []
        self.sizes: List[Optional[int]] = []
        self.pgid = None
        self.counted = False
        self.cancel = threading.Event()

    def view(self, level="BASIC"):
        if level == "MINIMAL":
            return {"id": self.id, "state": self.state}
        body = {"id": self.id, "state": self.state, "name": self.plan.name,
                "description": self.plan.description, "tags": self.plan.tags,
                "creation_time": self.created,
                "executors": [{"image": self.plan.image, "command": self.plan.command,
                               "workdir": WORKDIR}],
                "inputs": [{"url": i.url, "path": f"{WORKDIR}/{i.relative}",
                            "type": i.kind} for i in self.plan.inputs if i.url],
                "outputs": [{"url": o.url, "path": f"{WORKDIR}/{o.relative}",
                             "type": o.kind} for o in self.plan.outputs]}
        if level == "FULL":
            executor_log = {"start_time": self.started, "end_time": self.ended,
                            "stdout": self.stdout, "stderr": self.stderr}
            if self.exit_code is not None:
                executor_log["exit_code"] = self.exit_code
            body["logs"] = [{
                "start_time": self.started, "end_time": self.ended,
                "logs": [executor_log] if self.started else [],
                "outputs": [{"url": o.url, "path": f"{WORKDIR}/{o.relative}",
                             **({"size_bytes": str(size)} if size is not None else {})}
                            for o, size in zip(self.plan.outputs, self.sizes)],
                "system_logs": self.system_logs}]
        return body


class Full(Exception):
    """The queue is full."""


class Engine:
    """Tasks, their separate queue and their separate slots.

    Not the agent's `_slots()` or its run store: a task never waits for, takes the
    place of, or fills the room of a request the coordinator sent. It is also a
    process of its own, so nothing here can reach one.
    """

    def __init__(self, config, policy: Policy, runner):
        self.config = config
        self.policy = policy
        self.runner = runner
        self.tasks: "OrderedDict[str, Task]" = OrderedDict()
        self.active = 0
        self._slots = None
        self._lock = threading.Lock()
        self._running = set()

    def _semaphore(self):
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.config.max_concurrent)
        return self._slots

    def audit(self, event, task=None, **fields):
        if task is not None:
            fields.setdefault("task_id", task.id)
            fields.setdefault("caller", task.owner)
        audit.record(event, source=SOURCE, **fields)

    def submit(self, owner: str, plan: Plan) -> Task:
        with self._lock:
            if self.active >= self.config.max_queued + self.config.max_concurrent:
                raise Full()
            task = Task(owner, plan)
            task.counted = True
            self.active += 1
        try:
            self.audit(
                "tes.task.created", task, image=plan.image,
                command_sha256=hashlib.sha256(
                    json.dumps(plan.command).encode()).hexdigest(),
                inputs=[i.url or "inline" for i in plan.inputs],
                outputs=[o.url for o in plan.outputs])
        except audit.AuditError:
            self._release(task)
            raise
        with self._lock:
            self.tasks[task.id] = task
            self._prune()
        runner_task = asyncio.get_running_loop().create_task(self._drive(task))
        self._running.add(runner_task)
        runner_task.add_done_callback(self._running.discard)
        return task

    def _release(self, task):
        """Give back the place a task held in the queue and the slots, once."""
        with self._lock:
            if task.counted:
                task.counted = False
                self.active -= 1

    def _prune(self):
        finished = [key for key, task in self.tasks.items() if task.state in TERMINAL]
        while len(self.tasks) > MAX_KEPT_TASKS and finished:
            self.tasks.pop(finished.pop(0), None)

    def get(self, owner, task_id) -> Optional[Task]:
        task = self.tasks.get(task_id)
        return task if task is not None and task.owner == owner else None

    def list(self, owner, prefix="", state=None):
        return [t for t in reversed(self.tasks.values())
                if t.owner == owner and t.plan.name.startswith(prefix)
                and (not state or t.state == state)]

    def _advance(self, task, state, **fields):
        task.state = state
        if state in TERMINAL:
            task.ended = now()
        try:
            self.audit("tes.task.state", task, state=state, **fields)
        except audit.AuditError:
            # The run may not go on unrecorded, and may not stay unfinished.
            if state not in TERMINAL:
                raise

    def cancel(self, task: Task):
        if task.state in TERMINAL or task.state == "CANCELING":
            return task
        task.cancel.set()
        if task.state == "QUEUED":
            self._advance(task, "CANCELED")
            self._release(task)
        else:
            task.state = "CANCELING"
            if task.pgid is not None:
                try:
                    os.killpg(task.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return task

    async def _drive(self, task):
        try:
            async with self._semaphore():
                if task.cancel.is_set():
                    return
                await run_in_threadpool(self.execute, task)
        except BaseException as failure:            # noqa: BLE001
            if task.state not in TERMINAL:
                task.system_logs.append(f"{type(failure).__name__}: {failure}")
                self._advance(task, "SYSTEM_ERROR")
        finally:
            self._release(task)

    def execute(self, task: Task):
        """One task, start to finish, in a thread. Every way out ends in a state."""
        plan, config = task.plan, self.config
        budget = Budget(config.max_bytes)
        workspace = None
        try:
            self._advance(task, "INITIALIZING")
            workspace = make_workspace(config.run_root or None)
            stage_in(plan, workspace.path, budget)
            runner = self.runner.with_image(plan.image)
            body = snakefile(runner.snakefile_preamble(), plan, workspace.path)
            with open(os.path.join(workspace.path, "Snakefile"), "x") as stream:
                stream.write(body)
            if task.cancel.is_set():
                self._advance(task, "CANCELED")
                return
            task.started = now()
            self._advance(task, "RUNNING")
            result = runner.run(workspace, config.timeout_s,
                                on_start=lambda pgid: self._attached(task, pgid),
                                on_finish=lambda: setattr(task, "pgid", None))
            task.stdout, task.stderr = result.stdout[-MAX_LOG:], result.stderr[-MAX_LOG:]
            task.exit_code = result.returncode
            if task.cancel.is_set():
                self._advance(task, "CANCELED")
                return
            if result.returncode != 0:
                self._advance(task, "EXECUTOR_ERROR", exit_code=result.returncode)
                return
            task.sizes = stage_out(plan, workspace.path, budget, self.policy)
            self._advance(task, "COMPLETE", exit_code=0,
                          bytes=config.max_bytes - budget.left)
        except TooBig:
            task.system_logs.append(f"more than {config.max_bytes} bytes in and out")
            self._advance(task, "SYSTEM_ERROR", error="too_big")
        except Refused as refusal:
            task.system_logs.append(refusal.reason)
            self._advance(task, "SYSTEM_ERROR", error=refusal.reason[:200])
        except Exception as failure:                 # noqa: BLE001
            task.system_logs.append(f"{type(failure).__name__}: {failure}")
            self._advance(task, "SYSTEM_ERROR", error=type(failure).__name__)
        finally:
            if workspace is not None:
                try:
                    workspace.cleanup()
                except Exception:                    # noqa: BLE001
                    pass

    def _attached(self, task, pgid):
        task.pgid = pgid
        if task.cancel.is_set():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
