"""What the API process does for a node, beside serving the API.

A plain agent has no database and none of this runs. A node sets
BIOCHEF_EGRESS_MODE, and then its runs are mirrored into `run_summary`, because
the database is the only channel to the page (section 3.1).

The mirror never decides anything about a run. A run must not depend on the page
being up, so a failure here is logged and the run carries on.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import retention
from node import egress
from node.policy import LocalRules
from store import db, repo
from store.states import EGRESS_FINISHED, EgressState

logger = logging.getLogger(__name__)

MANIFEST_FILE = "run.json"

def mode():
    """`airlock`, `manual`, or None on an agent that is not a node."""
    value = (os.getenv("BIOCHEF_EGRESS_MODE") or "").strip()
    if not value:
        return None
    if value not in (egress.AIRLOCK, egress.MANUAL):
        raise ValueError("BIOCHEF_EGRESS_MODE must be airlock or manual")
    return value


def _utc():
    return datetime.now(timezone.utc)


class RunMirror:
    """Writes a run's state to `run_summary` as it changes."""

    def __init__(self, bind):
        self.bind = bind
        self._steps = {}
        self._started = {}

    def _step_rows(self, run):
        """Step states, with times this side stamps as they change."""
        known = self._steps.setdefault(run.run_id, {})
        now = _utc().isoformat()
        for name, state in run.step_status.items():
            row = known.setdefault(name, {"id": name, "state": state,
                                          "started_at": None, "ended_at": None})
            row["state"] = state
            if state == "RUNNING" and row["started_at"] is None:
                row["started_at"] = now
            if state in ("COMPLETE", "FAILED") and row["ended_at"] is None:
                row["ended_at"] = now
        return list(known.values())

    def __call__(self, run):
        try:
            self._save(run)
        except Exception:                            # noqa: BLE001
            # A database that is down must not stop a run that is working.
            logger.exception("could not mirror run %s", run.run_id)

    def _save(self, run):
        fields = {"state": run.state.value, "steps": self._step_rows(run),
                  "log_stdout": run.stdout, "log_stderr": run.stderr,
                  "node_logs": run.node_logs, "exit_code": run.exit_code}
        request_id = getattr(run, "request_id", None)
        if request_id is not None:
            fields["request_id"] = request_id
        if run.state.value == "INITIALIZING" and run.run_id not in self._started:
            self._started[run.run_id] = _utc()
            fields["started_at"] = self._started[run.run_id]
        if run.state.value in ("COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED"):
            fields["ended_at"] = _utc()
        with db.writing(self.bind) as session:
            repo.save_run(session, run.run_id, **fields)

    def workspace_kept(self, run_id, ws, manifest, outputs):
        """Record where a kept run lives, and make sure its manifest is there."""
        path = os.path.join(ws.path, MANIFEST_FILE)
        try:
            if not os.path.exists(path):
                ws.write_bytes(MANIFEST_FILE, _json_bytes(manifest or {}))
        except OSError:
            logger.exception("could not write the manifest of %s", run_id)
            path = None
        try:
            files = _listing(ws, outputs)
            with db.writing(self.bind) as session:
                repo.save_run(session, run_id, state=repo.get_run(session, run_id).state,
                              workspace_path=ws.path, manifest_path=path,
                              egress_files=files)
        except Exception:                            # noqa: BLE001
            logger.exception("could not record the workspace of %s", run_id)


def _json_bytes(document):
    return json.dumps(document, indent=2, sort_keys=True).encode()


def _listing(ws, outputs):
    """The output files of a run, with their sizes. Nothing has judged them yet."""
    names = sorted({name for handles in outputs.values() for name in handles.values()})
    rows = []
    for name in names:
        with ws.open_read(name) as handle:
            rows.append({"file": name, "size": os.fstat(handle.fileno()).st_size,
                         "verdict": None, "reason": None})
    return rows


def settle_lost_runs(session, now=None):
    """A run this process no longer holds is not running, whatever the row says.

    Runs live in memory, so a restart ends them. Left alone they would show as
    RUNNING for ever, with a Cancel button that nothing can carry out.
    """
    for row in repo.runs_not_finished(session):
        repo.save_run(session, row.run_id, state="SYSTEM_ERROR",
                      ended_at=now or _utc())


def rebuild_retention(session, retained, workspace_type, keep_seconds, now=None):
    """Workspaces are held in memory, so a restarted process reads them back here.

    A run still waiting for egress is held. One whose clock has started keeps what
    is left of it. A run with no egress keeps the clock it started at the run's end.
    """
    now = now or _utc()
    kept = 0
    for row in repo.runs_with_workspaces(session):
        finished = EgressState(row.egress_state) in EGRESS_FINISHED
        waiting = row.egress_state != EgressState.NOT_READY.value and not finished
        if row.request_id is not None and row.egress_state == EgressState.NOT_READY.value:
            waiting = True
        if row.retain_until is not None:
            kept_at = row.retain_until - timedelta(seconds=keep_seconds)
        elif row.ended_at is not None:
            kept_at = row.ended_at
        else:
            kept_at = now
        try:
            ws = workspace_type(row.workspace_path)
        except OSError:
            continue
        retained.adopt(row.run_id, ws,
                       None if waiting else kept_at.timestamp())
        kept += 1
    # Whatever expired while the process was down goes now.
    retained.workspace(None, now=now.timestamp())
    return kept


class NodeWorker:
    """The API process's side of the database channel, one pass at a time."""

    def __init__(self, bind, *, cancel, retained, keys, node, airlock=None, policy=None,
                 coordinator=None, workspace_of=None, keep_seconds=None):
        self.bind = bind
        self.cancel = cancel
        self.retained = retained
        self.keys = keys
        self.node = node
        self.airlock = airlock
        # Without the Policy Engine the agent checks what it can, and marks the
        # rest as not verified (node/policy.py).
        self.policy = policy or LocalRules()
        self.coordinator = coordinator
        self.workspace_of = workspace_of or retained.workspace
        self.keep_seconds = (retention.KEEP_OUTPUTS_SECONDS if keep_seconds is None
                             else keep_seconds)

    def tick(self, now=None):
        now = now or _utc()
        self._cancellations()
        with db.writing(self.bind) as session:
            for row in repo.runs_ready_for_egress(session):
                needs_airlock = mode() == egress.AIRLOCK
                if needs_airlock and self.airlock is None:
                    continue    # Nothing skips a stage: the run waits.
                egress.start(session, row.run_id, self.policy, self.airlock,
                             mode=mode(), now=now, keep_seconds=self.keep_seconds)
        if self.airlock is not None:
            with db.writing(self.bind) as session:
                egress.poll_airlock(session, self.airlock, self.keys, node=self.node,
                                    now=now,
                                    keep_seconds=self.keep_seconds)
        if self.coordinator is not None:
            with db.writing(self.bind) as session:
                egress.send_released(session, self.keys, self.coordinator,
                                     self.workspace_of, node=self.node, now=now,
                                     keep_seconds=self.keep_seconds)
        self._clocks(now)

    def _cancellations(self):
        with db.reading(self.bind) as session:
            wanted = [row.run_id for row in repo.runs_to_cancel(session)]
        for run_id in wanted:
            self.cancel(run_id)

    def _clocks(self, now):
        with db.reading(self.bind) as session:
            finished = [(row.run_id, row.retain_until)
                        for row in repo.runs_with_workspaces(session)
                        if EgressState(row.egress_state) in EGRESS_FINISHED]
        for run_id, until in finished:
            if until is not None:
                self.retained.start_clock(
                    run_id, (until - timedelta(seconds=self.keep_seconds)).timestamp())
