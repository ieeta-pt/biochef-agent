"""A run that outlives the request that asked for it (#5).

B1 uses the eight WES-style `RunState` names required by issue #5. A complete
WES API remains a separate endpoint task.

The store is in memory and deliberately small in ambition. It is enough for
"submit, poll, collect", which is what B1 asks for, and it is honest about what
it is not: nothing survives a restart, and nothing is shared between processes.
Both are written down rather than discovered later, and both are the reason a
persistent store is its own piece of work.
"""

import os
import threading
import uuid
from collections import OrderedDict
from enum import Enum

from steplogs import clamp
from typing import Optional


class RunState(str, Enum):
    """The eight WES-style run states required by issue #5.

    str-valued so a state serialises as its own name in JSON without anything
    having to convert it, and so a comparison against the string works.
    """

    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"


TERMINAL = frozenset({RunState.COMPLETE, RunState.EXECUTOR_ERROR,
                      RunState.SYSTEM_ERROR, RunState.CANCELED})

#: Which states may follow which. Absent from here means it may not happen.
#:
#: Written out rather than left implicit because the interesting bugs in a state
#: machine are the transitions nobody thought about -- a run going back to
#: RUNNING after it failed, or reporting COMPLETE after it was canceled. A
#: terminal state has no successors at all.
ALLOWED = {
    RunState.QUEUED: {RunState.INITIALIZING, RunState.CANCELING,
                      RunState.SYSTEM_ERROR, RunState.CANCELED},
    RunState.INITIALIZING: {RunState.RUNNING, RunState.CANCELING,
                            RunState.EXECUTOR_ERROR, RunState.SYSTEM_ERROR},
    RunState.RUNNING: {RunState.COMPLETE, RunState.EXECUTOR_ERROR,
                       RunState.SYSTEM_ERROR, RunState.CANCELING},
    RunState.CANCELING: {RunState.CANCELED, RunState.SYSTEM_ERROR},
    RunState.COMPLETE: set(),
    RunState.EXECUTOR_ERROR: set(),
    RunState.SYSTEM_ERROR: set(),
    RunState.CANCELED: set(),
}


class UnknownRun(KeyError):
    """No run by that id, or it has been evicted."""


class IllegalTransition(Exception):
    """A state change the machine does not permit."""


class RunCapacityError(Exception):
    """This process cannot retain another run."""


MAX_RUNS = int(os.getenv("BIOCHEF_MAX_RUNS", "256"))
"""How many runs are remembered.

Bounded because this is a dictionary that only ever grew otherwise, and a
long-lived service accepting runs would use memory in proportion to its uptime.
When it is full the oldest FINISHED run is forgotten; if every slot is still
in flight, a new submission is refused instead of evicting live work.
"""

if MAX_RUNS < 1:
    raise ValueError("BIOCHEF_MAX_RUNS must be positive")


class KeyInFlight(Exception):
    """A request with this idempotency key is still being admitted (#87).

    Two retries racing is what retrying looks like, so this is not a mistake in
    the caller -- but the agent cannot yet say which run the key belongs to,
    and inventing a second one is the thing being prevented.
    """


class Replay(Exception):
    """This idempotency key already created a run (#87).

    Carries the run so the caller's body can be compared against its
    fingerprint before the run is handed back.
    """

    def __init__(self, run):
        super().__init__(run.run_id)
        self.run = run


class Run:
    """One submitted workflow, and whatever is known about it so far."""

    def __init__(self, run_id: str, request_id: str = None):
        self.run_id = run_id
        self.request_id = request_id
        """The id of the request that asked for this run, when there was one.

        A hub whose submission response was lost in transit -- a timeout, a
        proxy hiccup -- never learned the run_id, and the run is then executing
        here and unknown there. Carrying the caller's own id back means it can
        recognise the run as the one it asked for.

        None for a run created without a request behind it, which is every run
        in a test that builds a store directly.
        """
        self.idempotency_key = None
        """The key that created this run, when the caller offered one (#87).

        Set by RunStore.create, which also indexes it. Kept on the record
        rather than only in the index so eviction cannot leave the two
        disagreeing: whatever drops the run drops the key.
        """
        self.fingerprint = None
        """What the submission that created this run looked like.

        None until the uploads have been read -- a run exists before its body
        does, because admission is refused before a large body is read. A key
        whose run has no fingerprint yet is a submission still being admitted,
        which is why a concurrent retry gets told to wait rather than given
        this run.
        """
        self.state = RunState.QUEUED
        self.outputs = None
        self.error = None
        self.stdout = ""
        self.stderr = ""
        self.failed_steps = {}
        self.step_status = {}
        self.node_logs = {}
        self.pgid = None
        """The process group executing this run, once there is one.

        A run waiting for a slot has none yet, and cancelling it is a matter of
        state alone. A run that has started needs the group ended, which is the
        same lever the timeout pulls.
        """

    def logs_as_dict(self) -> dict:
        """Run-level output, diagnostic error blocks, and per-node output.

        `failed_steps` is populated only on a failed workflow, from Snakemake-style
        stderr headings. Tool output can imitate those headings. `node_logs`
        comes from separate files opened for each executed rule.
        """
        body = {
            "run_id": self.run_id,
            "state": self.state.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "failed_steps": self.failed_steps,
            "node_logs": self.node_logs,
        }
        if self.request_id is not None:
            # Same as as_dict. This endpoint already repeats run_id and state,
            # and a hub fetching logs should not have to fetch the run as well
            # to learn which of its submissions they belong to.
            body["request_id"] = self.request_id
        return body

    def as_dict(self) -> dict:
        """What a caller is told about this run.

        Outputs appear only once there are any, and there only ever are on
        COMPLETE -- nothing else sets them. An earlier version took an
        include_outputs flag that no caller ever passed as False; a dead
        parameter guarding a security-shaped property reads like a control and
        is not one.
        """
        body = {"run_id": self.run_id, "state": self.state.value}
        if self.request_id is not None:
            # Present whenever a request asked for this run, which is every run
            # the route creates. Absent rather than null when there was none, to
            # match how error and outputs behave.
            body["request_id"] = self.request_id
        if self.idempotency_key is not None:
            # So a caller holding a key can confirm the run it got back is the
            # one that key created, rather than taking it on trust.
            body["idempotency_key"] = self.idempotency_key
        if self.step_status:
            # What the editor paints on each node. Present as soon as the
            # workflow starts, because the output is read as it arrives rather
            # than at the end.
            body["steps"] = dict(self.step_status)
        if self.error is not None:
            body["error"] = self.error
        if self.outputs is not None:
            body["outputs"] = self.outputs
        return body


class RunStore:
    """Somewhere to put runs, safe to touch from more than one thread.

    The lock is not decoration: a run is created on the event loop, advanced
    from a worker thread, and read by whatever request happens to poll it. All
    three can overlap.
    """

    def __init__(self, max_runs: int = None):
        self._runs = OrderedDict()
        self._by_key = {}
        """Idempotency key -> run_id, for the runs still retained (#87).

        Not a second store with its own bound. Every entry here points at a
        live entry in _runs, and _evict_if_needed and discard remove both
        together under this lock -- so the window in which a key is honoured
        is exactly the retention window that is already documented, and there
        is no way for the index to outlive what it indexes or to grow without
        limit.
        """
        self._lock = threading.Lock()
        self._max = MAX_RUNS if max_runs is None else max_runs
        if self._max < 1:
            raise ValueError("max_runs must be positive")

    def create(self, request_id: str = None, idempotency_key: str = None):
        """Admit a run, or refuse because this key already admitted one.

        Looking the key up and creating the run have to be ONE operation under
        this lock. Two concurrent retries that each checked first and then
        created would both find nothing and both create -- which is the exact
        duplicate this exists to prevent, and it is the likeliest shape for a
        retry to arrive in.

        Raises Replay when the key already has a run, carrying it so the
        caller can compare bodies before handing it back; KeyInFlight when the
        key has a run whose body has not been read yet, because the agent
        cannot yet say whether this is the same submission; RunCapacityError
        as before.

        A key is recorded only once the run exists, and the capacity refusal
        happens before that, so a request refused with 503 does not burn its
        key -- a transient refusal must not become a permanent one.
        """
        with self._lock:
            if idempotency_key is not None:
                prior = self._runs.get(self._by_key.get(idempotency_key))
                if prior is not None:
                    if prior.fingerprint is None:
                        raise KeyInFlight(idempotency_key)
                    raise Replay(prior)

            # Eviction cannot take this key's run: the check above means the
            # key has none, so there is nothing here to protect from it.
            self._evict_if_needed()
            run = Run(uuid.uuid4().hex, request_id=request_id)
            run.idempotency_key = idempotency_key
            self._runs[run.run_id] = run
            if idempotency_key is not None:
                self._by_key[idempotency_key] = run.run_id
        return run

    def settle_fingerprint(self, run_id: str, value: str) -> None:
        """Record what the submission looked like, once its body has been read.

        Until this happens the run's key reads as in flight, so a concurrent
        retry is told to wait rather than handed a run whose body is unknown.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.fingerprint = value

    def discard(self, run_id: str) -> None:
        """Undo admission when request upload fails before a run is launched.

        Takes the key index with it, or a submission that never ran would hold
        its key for as long as the store lives and every retry would be told
        the work is already done.
        """
        with self._lock:
            run = self._runs.pop(run_id)
            self._forget_key(run)

    def get(self, run_id: str) -> Run:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise UnknownRun(run_id) from None

    def advance(self, run_id: str, state: RunState, *,
                outputs=None, error: Optional[str] = None) -> Run:
        """Move a run to a new state, refusing one the machine does not allow.

        Refusing rather than tolerating: a transition that should not happen is
        a bug in the caller, and letting it through would leave a run claiming
        something untrue about itself -- COMPLETE after a failure being the one
        that matters, since a client would then go looking for outputs.
        """
        with self._lock:
            try:
                run = self._runs[run_id]
            except KeyError:
                raise UnknownRun(run_id) from None

            if state not in ALLOWED[run.state]:
                raise IllegalTransition(
                    f"{run_id}: {run.state.value} -> {state.value} is not a "
                    f"transition this run may make"
                )

            run.state = state
            if outputs is not None:
                run.outputs = outputs
            if error is not None:
                run.error = error
            return run

    def attach(self, run_id: str, pgid: int) -> bool:
        """Record the group and report if cancellation arrived before it did."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.pgid = pgid
                return run.state is RunState.CANCELING
            return False

    def record_progress(self, run_id: str, step_status) -> None:
        """Per-step status, replaced wholesale as it changes.

        Called from the reader thread while the workflow is still running, so it
        takes the lock like everything else here.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.step_status = dict(step_status)

    def record_logs(self, run_id: str, stdout, stderr, steps,
                    node_logs=None) -> None:
        """Keep bounded stream tails, node logs, and diagnostic blocks.

        The attribution arrives finished rather than being worked out here.
        Doing it in this module would mean importing the emitter for its rule
        naming, and the emitter builds a registry client at import -- so asking
        a run store what state a run is in would open a connection.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.stdout = clamp(stdout)
            run.stderr = clamp(stderr)
            run.failed_steps = steps
            run.node_logs = node_logs or {}

    def detach(self, run_id: str) -> None:
        """Forget the process group, because it no longer exists.

        The number outlives the group, and the kernel may reissue it. Anything
        still holding it is aiming at whoever gets it next.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.pgid = None

    def _forget_key(self, run) -> None:
        """Called with the lock held. Drop a run's key, if it still owns it.

        Checked rather than assumed: a key is reassigned when the run it
        pointed at has been evicted and a retry creates a new one, and the
        later run then owns the entry. Deleting unconditionally would remove
        the live run's key when the stale one is discarded.
        """
        key = getattr(run, "idempotency_key", None)
        if key is not None and self._by_key.get(key) == run.run_id:
            del self._by_key[key]

    def _evict_if_needed(self):
        """Called with the lock held."""
        while len(self._runs) >= self._max:
            for run_id, run in self._runs.items():
                if run.state in TERMINAL:
                    del self._runs[run_id]
                    self._forget_key(run)
                    break
            else:
                raise RunCapacityError("all remembered runs are still in flight")
