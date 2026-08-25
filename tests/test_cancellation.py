"""Whether a run can be stopped once it has started (#7).

Before this, it could not.

CANCELING and CANCELED are in the vocabulary and in the transition table --
QUEUED and INITIALIZING and RUNNING all list CANCELING as a legal successor, and
CANCELING lists CANCELED. Nothing produces either. The state machine describes a
capability the service does not have, which is worse than not describing it: a
client reading the states, or a WES adapter generated from them, would conclude
that cancellation exists.

So a run that is going wrong runs to completion, or until BIOCHEF_RUN_TIMEOUT --
fifteen minutes by default. The only way to stop one sooner is to stop the
service, which stops every other run with it.

The machinery is already there. The runner puts each run in its own process
group precisely so the timeout can kill the group rather than the child; nothing
but the timeout can pull that lever.
"""

import inspect
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import main
import runner as runner_module
from runs import ALLOWED, RunState


import json
import os
import signal
import time

import pytest

import convert
from runs import ALLOWED, RunState, RunStore

BUNDLE = {"id": "tool", "name": "tool", "bin": "tool",
          "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
                 "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
          "parameters": []}

WORKFLOW = json.dumps({
    "nodes": [
        {"id": "input-1", "type": "inputWorkflowNode", "data": {}},
        {"id": "tool-1", "type": "workflowNode",
         "data": {"label": "tool", "repo": "r", "paramValues": {}, "outputs": {}}},
        {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
    ],
    "edges": [
        {"source": "input-1", "sourceHandle": "out",
         "target": "tool-1", "targetHandle": "in"},
        {"source": "tool-1", "sourceHandle": "out",
         "target": "output-1", "targetHandle": "in"},
    ],
})


def _digest(payload):
    import hashlib
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _Registry:
    def __init__(self):
        self.bundle_bytes = json.dumps(BUNDLE).encode()
        self.binary_bytes = b"#!/bin/sh\n"

    def get_container(self, target):
        return target

    def get_manifest(self, container, *a, **k):
        return {"layers": [
            {"digest": _digest(self.bundle_bytes),
             "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "bundle.json"}},
            {"digest": _digest(self.binary_bytes),
             "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "tool"}},
        ]}

    def pull(self, target, outdir):
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "bundle.json"), "wb") as f:
            f.write(self.bundle_bytes)
        with open(os.path.join(outdir, "tool"), "wb") as f:
            f.write(self.binary_bytes)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry())
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "RUNS", RunStore())
    convert.tools.clear()
    yield main.RUNS
    convert.tools.clear()


def _submit(client):
    return client.post("/runs", data={"biochef_workflow": WORKFLOW},
                       files=[("files", ("input-1-out", b"in",
                                         "application/octet-stream"))])


def _wait_for(store, run_id, states, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if store.get(run_id).state in states:
            return store.get(run_id).state
        time.sleep(0.02)
    return store.get(run_id).state


# --------------------------------------------------------------------------
# the vocabulary is now honest


def test_the_states_are_reachable_in_the_table_and_in_the_service():
    """They were in the table before, and nothing produced them.

    A state machine describing a capability the service does not have is worse
    than one that does not mention it: a client reading the states, or a WES
    adapter generated from them, concludes cancellation exists.
    """
    assert RunState.CANCELING in ALLOWED[RunState.RUNNING]
    assert RunState.CANCELED in ALLOWED[RunState.CANCELING]

    service = Path(REPO_ROOT / "main.py").read_text()
    assert "CANCELING" in service and "CANCELED" in service


def test_the_path_is_the_one_wes_uses():
    """So exposing this as WES later (F5) does not move it."""
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/runs/{run_id}/cancel" in paths, sorted(p for p in paths if p)


# --------------------------------------------------------------------------
# cancelling a run that is executing


def test_a_running_run_is_cancelled_and_its_processes_end(service, monkeypatch):
    """The tool AND whatever it spawned.

    A tool that starts a child and outlives its parent is the reason the runner
    puts each run in its own process group. Cancelling has to pull that same
    lever, or a cancelled run leaves work running on the host.
    """
    from fastapi.testclient import TestClient

    grandchild = {}

    def spawning(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        import subprocess
        script = os.path.join(ws.path, "slow.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    "sh -c 'echo $$ > grandchild.pid; exec sleep 300' &\n"
                    "wait\n")
        os.chmod(script, 0o755)
        process = subprocess.Popen([script], cwd=ws.path, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        pgid = os.getpgid(process.pid)
        if on_start is not None:
            on_start(pgid)
        deadline = time.time() + 10
        pid_file = os.path.join(ws.path, "grandchild.pid")
        while time.time() < deadline:
            try:
                grandchild["pid"] = int(open(pid_file).read().strip())
                break
            except (FileNotFoundError, ValueError):
                time.sleep(0.01)
        try:
            out, err = process.communicate()
        finally:
            # Never leave the group behind. Earlier versions of this test left
            # orphaned `sleep 300` groups on the host when the run took an
            # unexpected path, which an audit found still running.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if on_finish is not None:
                on_finish()
        return process.returncode, out, err

    monkeypatch.setattr(main, "run_snakemake", spawning)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait_for(service, run_id, {RunState.RUNNING})

        deadline = time.time() + 10
        while "pid" not in grandchild and time.time() < deadline:
            time.sleep(0.02)
        assert "pid" in grandchild, "the tool never spawned its child"

        response = client.post(f"/runs/{run_id}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == RunState.CANCELING.value, (
            "the kill has been issued but the run is not over until the worker "
            "says so; claiming CANCELED here would be claiming what has not "
            "happened"
        )

        assert _wait_for(service, run_id, {RunState.CANCELED}) is RunState.CANCELED

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(grandchild["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the child outlived the cancellation")


def test_a_run_that_finishes_after_being_cancelled_still_reports_CANCELED(
        service, monkeypatch):
    """The kill can lose the race, and the answer is still CANCELED.

    Reporting COMPLETE would hand back outputs the caller has just said they do
    not want. The stub here never registers a process group, so nothing is
    killed at all -- which is the worst case for this rule and therefore the
    right one to test.

    Built with events rather than a module global. An earlier version set a
    "_current_for_test" attribute on main and submitted twice, so its first
    submission ran the REAL run_snakemake -- a test that reached for the actual
    snakemake binary to check a state transition.
    """
    import threading

    from fastapi.testclient import TestClient

    started = threading.Event()
    release = threading.Event()

    def waits_then_succeeds(ws, timeout_s=None, on_start=None, **kwargs):
        started.set()
        release.wait(15)
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"produced anyway")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", waits_then_succeeds)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert started.wait(15), "the run never started"

        assert client.post(f"/runs/{run_id}/cancel").status_code == 200
        release.set()

        state = _wait_for(service, run_id, {RunState.CANCELED, RunState.COMPLETE})
        body = client.get(f"/runs/{run_id}").json()

    assert state is RunState.CANCELED, body
    assert "outputs" not in body, "a cancelled run handed back its outputs"


# --------------------------------------------------------------------------
# cancelling a run that has not started, and refusing the impossible


def test_a_queued_run_is_cancelled_without_ever_executing(service, monkeypatch):
    """Nothing ran, so there is nothing to kill -- only a state to settle."""
    import asyncio

    from fastapi.testclient import TestClient

    executed = {"count": 0}

    def counting(ws, timeout_s=None, on_start=None, **kwargs):
        executed["count"] += 1
        time.sleep(0.4)
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"x")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", counting)
    monkeypatch.setattr(main, "MAX_CONCURRENT_RUNS", 1)
    main._slots_by_loop.clear()

    try:
        with TestClient(main.app) as client:
            first = _submit(client).json()["run_id"]
            queued = _submit(client).json()["run_id"]
            _wait_for(service, first, {RunState.RUNNING})

            assert service.get(queued).state is RunState.QUEUED
            assert client.post(f"/runs/{queued}/cancel").status_code == 200
            assert _wait_for(service, queued, {RunState.CANCELED}) is RunState.CANCELED
            _wait_for(service, first, {RunState.COMPLETE})
    finally:
        main.MAX_CONCURRENT_RUNS = 4
        main._slots_by_loop.clear()

    assert executed["count"] == 1, (
        f"the cancelled run executed anyway ({executed['count']} executions)"
    )


def test_cancelling_a_finished_run_is_refused(service, monkeypatch):
    """409, not a cheerful 200. There is nothing to cancel."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "run_snakemake",
                        lambda ws, timeout_s=None, on_start=None, **k: (
                            open(os.path.join(ws.path, "tool-1-out"), "wb").write(b"x"),
                            0, "", "")[1:])

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait_for(service, run_id, {RunState.COMPLETE})
        response = client.post(f"/runs/{run_id}/cancel")

    assert response.status_code == 409, response.text
    assert "already" in response.text


def test_cancelling_an_unknown_run_is_404(service):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        assert client.post("/runs/nope/cancel").status_code == 404


def test_cancel_aims_at_nothing_once_the_child_has_been_reaped(service, monkeypatch):
    """The window between the process ending and the state saying so.

    A run is still RUNNING while its outputs are read, base64'd and its
    workspace removed -- measured between 0.01s and 1.14s depending on output
    size. The process group is already gone by then, and a process group id is a
    number the kernel reissues once the group is empty.

    So a cancel arriving in that window used to SIGKILL whoever had inherited
    the number. killpg reaches only group LEADERS, which narrows the victims --
    to login shells, service units, and above all this service's OWN later runs,
    every one of which is a leader by construction and whose creation rate rises
    with load. One run silently killing another's snakemake, reported as an
    unexplained error, is the realistic outcome.

    The runner now takes the id back the instant it reaps the child, so by the
    time this window opens there is nothing to aim at.
    """
    import subprocess
    import threading

    from fastapi.testclient import TestClient

    killed = []
    monkeypatch.setattr(main.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))

    reaped = threading.Event()
    release = threading.Event()

    def reap_then_stall(ws, timeout_s=None, on_start=None, on_finish=None, **kw):
        process = subprocess.Popen(["sh", "-c", "exit 0"], cwd=ws.path,
                                   start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        pgid = os.getpgid(process.pid)
        if on_start is not None:
            on_start(pgid)
        process.communicate()                 # the child is gone and reaped
        if on_finish is not None:
            on_finish()
        reaped.set()
        release.wait(15)                      # stand in for the post-run work
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"x")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", reap_then_stall)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert reaped.wait(15), "the stub never ran"

        assert service.get(run_id).state is RunState.RUNNING, (
            "the window this test is about did not open"
        )
        assert service.get(run_id).pgid is None, (
            "the group id survived the process it names"
        )

        assert client.post(f"/runs/{run_id}/cancel").status_code == 200
        release.set()
        _wait_for(service, run_id, {RunState.CANCELED})

    assert killed == [], (
        f"cancel sent {killed} at a group that no longer existed; the number "
        f"may belong to something else by now"
    )
