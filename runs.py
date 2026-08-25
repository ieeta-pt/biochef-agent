"""A run that outlives the request that asked for it (#5).

B1 wants the WES `RunState` vocabulary rather than one of our own, so that
exposing this as a WES endpoint later (F5) is an adapter and not a rewrite. The
names below are exactly WES's, spelled the same way.

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
    """The WES run states, verbatim.

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


MAX_RUNS = int(os.getenv("BIOCHEF_MAX_RUNS", "256"))
"""How many runs are remembered.

Bounded because this is a dictionary that only ever grew otherwise, and a
long-lived service accepting runs would use memory in proportion to its uptime.
When it is full the oldest FINISHED run is forgotten; a run still in flight is
never evicted, because forgetting it would lose the only handle to work that is
still happening.
"""


class Run:
    """One submitted workflow, and whatever is known about it so far."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.state = RunState.QUEUED
        self.outputs = None
        self.error = None
        self.stdout = ""
        self.stderr = ""
        self.failed_steps = {}
        self.step_status = {}
        self.pgid = None
        """The process group executing this run, once there is one.

        A run waiting for a slot has none yet, and cancelling it is a matter of
        state alone. A run that has started needs the group ended, which is the
        same lever the timeout pulls.
        """

    def logs_as_dict(self) -> dict:
        """What this run printed, and which steps failed.

        `steps` is only ever the failing ones, and the docstring on steplogs
        explains why: snakemake attributes failures by name and does not
        separate anything else.
        """
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "failed_steps": self.failed_steps,
        }

    def as_dict(self) -> dict:
        """What a caller is told about this run.

        Outputs appear only once there are any, and there only ever are on
        COMPLETE -- nothing else sets them. An earlier version took an
        include_outputs flag that no caller ever passed as False; a dead
        parameter guarding a security-shaped property reads like a control and
        is not one.
        """
        body = {"run_id": self.run_id, "state": self.state.value}
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
        self._lock = threading.Lock()
        self._max = MAX_RUNS if max_runs is None else max_runs

    def create(self) -> Run:
        run = Run(uuid.uuid4().hex)
        with self._lock:
            self._evict_if_needed()
            self._runs[run.run_id] = run
        return run

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

    def attach(self, run_id: str, pgid: int) -> None:
        """Record the process group, so something can end it later."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.pgid = pgid

    def record_progress(self, run_id: str, step_status) -> None:
        """Per-step status, replaced wholesale as it changes.

        Called from the reader thread while the workflow is still running, so it
        takes the lock like everything else here.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.step_status = dict(step_status)

    def record_logs(self, run_id: str, stdout, stderr, steps) -> None:
        """Keep what the run printed, bounded, with failures already attributed.

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

    def detach(self, run_id: str) -> None:
        """Forget the process group, because it no longer exists.

        The number outlives the group, and the kernel may reissue it. Anything
        still holding it is aiming at whoever gets it next.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.pgid = None

    def _evict_if_needed(self):
        """Called with the lock held."""
        while len(self._runs) >= self._max:
            for run_id, run in self._runs.items():
                if run.state in TERMINAL:
                    del self._runs[run_id]
                    break
            else:
                # Everything remembered is still in flight. Forgetting one would
                # lose the only handle to work that is still happening, so the
                # store grows past its bound rather than doing that, and the
                # ceiling is enforced again as soon as anything finishes.
                return
