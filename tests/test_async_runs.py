"""What happens to the connection during a long run (#5).

Before this, for the whole thing. /convert still is -- it is the contract the
editor speaks -- and /runs is the same work without the wait.

B1 asks two questions to verify first: is the current REST API synchronous, and
what happens to the HTTP connection during a long run. Yes, and it waits.

/convert does the whole job inside one request: it creates the workspace, pulls
the tools, writes the Snakefile, runs snakemake to completion, reads the outputs
back, base64-encodes them into the response, and only then replies. The default
timeout is 900 seconds, so a single request can legitimately hold a socket for
fifteen minutes and return nothing until the end.

What that costs:

  the client must wait          with no way to reconnect, and no way to ask how
                                far along it is
  a dropped connection loses    nothing survives the request, so a network blip
  everything                    means the work is gone
  a run has no identity         there is nothing to refer to afterwards, which
                                is why cancellation (#7), per-step logs (#6) and
                                progress (#8) all wait on this
  nothing can be polled         there is no second endpoint at all

And it is precisely wrong for what this service is for. Dispatching a step to an
HPC queue means waiting for a scheduler, not for a subprocess; there is no
version of that which fits inside one HTTP request.
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


import json
import time

import pytest

import convert
from runs import ALLOWED, TERMINAL, IllegalTransition, RunState, RunStore, UnknownRun

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
            {"digest": _digest(self.bundle_bytes), "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "bundle.json"}},
            {"digest": _digest(self.binary_bytes), "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "tool"}},
        ]}

    def pull(self, target, outdir):
        import os
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "bundle.json"), "wb") as f:
            f.write(self.bundle_bytes)
        with open(os.path.join(outdir, "tool"), "wb") as f:
            f.write(self.binary_bytes)


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A service whose runs succeed without touching a registry or snakemake."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry())
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    convert.tools.clear()

    def fake_run(ws, timeout_s=None, **kwargs):
        import os
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"the output")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", fake_run)
    monkeypatch.setattr(main, "RUNS", RunStore())
    yield main.RUNS
    convert.tools.clear()


def _submit(client):
    return client.post("/runs", data={"biochef_workflow": WORKFLOW},
                       files=[("files", ("input-1-out", b"in",
                                         "application/octet-stream"))])


def _poll(client, run_id, until=TERMINAL, tries=200):
    for _ in range(tries):
        body = client.get(f"/runs/{run_id}").json()
        if body["state"] in {s.value for s in until}:
            return body
        time.sleep(0.02)
    return client.get(f"/runs/{run_id}").json()


# --------------------------------------------------------------------------
# submitting and polling


def test_submitting_answers_immediately_with_something_to_poll(service):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = _submit(client)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"]
    assert body["state"] in {s.value for s in RunState}
    assert "outputs" not in body, "a run that has not finished has no outputs"


def test_a_run_reaches_COMPLETE_and_carries_its_outputs(service):
    import base64

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        body = _poll(client, run_id)

    assert body["state"] == RunState.COMPLETE.value, body
    assert body["outputs"]["tool-1"]["out"] == base64.b64encode(b"the output").decode()


def test_a_failing_run_reaches_EXECUTOR_ERROR_and_says_why(service, monkeypatch):
    """A tool exiting non-zero is the run's failure, not the service's."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "run_snakemake",
                        lambda ws, timeout_s=None, **kw: (2, "", "it went wrong"))

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        body = _poll(client, run_id)

    assert body["state"] == RunState.EXECUTOR_ERROR.value, body
    assert "error" in body
    assert "outputs" not in body


def test_a_defect_in_the_service_is_SYSTEM_ERROR_not_the_workflows_fault(service, monkeypatch):
    from fastapi.testclient import TestClient

    def boom(ws, timeout_s=None, **kwargs):
        raise RuntimeError("a bug in this service")

    monkeypatch.setattr(main, "run_snakemake", boom)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        body = _poll(client, run_id)

    assert body["state"] == RunState.SYSTEM_ERROR.value, body


def test_an_unknown_run_is_404_not_500(service):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get("/runs/does-not-exist")

    assert response.status_code == 404
    assert "no run" in response.text


def test_the_synchronous_endpoint_still_works(service):
    """/convert is the contract the editor speaks; this must not have moved."""
    import base64

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.post("/convert", data={"biochef_workflow": WORKFLOW},
                               files=[("files", ("input-1-out", b"in",
                                                 "application/octet-stream"))])

    assert response.status_code == 200, response.text
    assert response.json()["tool-1"]["out"] == base64.b64encode(b"the output").decode()


# --------------------------------------------------------------------------
# the state machine


def test_every_wes_state_exists_and_is_spelled_the_same():
    """WES's vocabulary verbatim, so F5 is an adapter rather than a rewrite."""
    assert {s.value for s in RunState} == {
        "QUEUED", "INITIALIZING", "RUNNING", "COMPLETE",
        "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELING", "CANCELED",
    }


def test_a_terminal_state_has_no_successors():
    """A run must not report COMPLETE after failing, or run again after ending."""
    for state in TERMINAL:
        assert ALLOWED[state] == set(), state


@pytest.mark.parametrize("first,second", [
    (RunState.COMPLETE, RunState.RUNNING),
    (RunState.EXECUTOR_ERROR, RunState.COMPLETE),
    (RunState.SYSTEM_ERROR, RunState.RUNNING),
    (RunState.QUEUED, RunState.COMPLETE),
    (RunState.QUEUED, RunState.RUNNING),
])
def test_an_illegal_transition_is_refused(first, second):
    """Refused rather than tolerated.

    A run allowed to move from EXECUTOR_ERROR to COMPLETE would claim something
    untrue about itself, and a client would go looking for outputs that were
    never produced.
    """
    store = RunStore()
    run = store.create()
    if first is not RunState.QUEUED:
        store.advance(run.run_id, RunState.INITIALIZING)
        if first is not RunState.INITIALIZING:
            store.advance(run.run_id, RunState.RUNNING)
            store.advance(run.run_id, first)

    with pytest.raises(IllegalTransition):
        store.advance(run.run_id, second)


def test_advancing_a_run_that_does_not_exist_is_not_a_silent_success():
    store = RunStore()
    with pytest.raises(UnknownRun):
        store.advance("nope", RunState.RUNNING)


def test_the_store_is_bounded_and_forgets_finished_runs_first():
    """It only ever grew before, so a long-lived service used memory in
    proportion to its uptime."""
    store = RunStore(max_runs=3)
    finished = []
    for _ in range(3):
        run = store.create()
        store.advance(run.run_id, RunState.INITIALIZING)
        store.advance(run.run_id, RunState.RUNNING)
        store.advance(run.run_id, RunState.COMPLETE)
        finished.append(run.run_id)

    store.create()
    with pytest.raises(UnknownRun):
        store.get(finished[0])
    assert store.get(finished[-1]) is not None


def test_a_run_still_in_flight_is_never_forgotten():
    """Evicting one would lose the only handle to work that is still happening."""
    store = RunStore(max_runs=2)
    in_flight = [store.create().run_id for _ in range(2)]
    for run_id in in_flight:
        store.advance(run_id, RunState.INITIALIZING)

    store.create()
    for run_id in in_flight:
        assert store.get(run_id).state is RunState.INITIALIZING


# --------------------------------------------------------------------------
# the document B1 asks to be the source of truth


def test_the_committed_openapi_document_is_current():
    """A spec that has drifted is worse than none, because clients come from it.

    Comparing the committed file against what the app serves means a route
    added without regenerating fails here rather than shipping a document that
    lies.

    Regenerate with: python ci/export_openapi.py
    """
    import importlib.util

    committed = REPO_ROOT / "openapi.json"
    assert committed.exists(), "missing; run python ci/export_openapi.py"

    spec = importlib.util.spec_from_file_location(
        "_export_openapi", REPO_ROOT / "ci" / "export_openapi.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    assert committed.read_text() == exporter.document(), (
        "openapi.json is out of date; run python ci/export_openapi.py"
    )


def test_the_document_describes_both_ways_of_running_a_workflow():
    """The synchronous contract and the asynchronous one, both."""
    document = json.loads((REPO_ROOT / "openapi.json").read_text())
    assert "post" in document["paths"]["/convert"]
    assert "post" in document["paths"]["/runs"]
    assert "get" in document["paths"]["/runs/{run_id}"]


# --------------------------------------------------------------------------
# what happens when more than one run is asked for at once


def test_many_runs_submitted_at_once_all_succeed(service):
    """Concurrency is the point of this endpoint, so it has to survive it.

    Twenty simultaneous submissions used to leave one COMPLETE and nineteen
    SYSTEM_ERROR: every run pulled the same tool into the same fixed .part
    directory, and they deleted each other's work --

      [Errno 17] File exists: .../cache/tool.part
      [Errno  2] No such file or directory: .../cache/tool.part

    The race predates this endpoint -- two concurrent /convert calls could
    always hit it -- but nothing made it easy to reach until runs could be
    submitted without waiting.
    """
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        run_ids = [_submit(client).json()["run_id"] for _ in range(20)]
        states = [_poll(client, run_id)["state"] for run_id in run_ids]

    assert states == [RunState.COMPLETE.value] * 20, {
        state: states.count(state) for state in set(states)
    }


def test_a_background_task_is_referenced_until_it_finishes():
    """asyncio.create_task returns a task the caller must keep.

    The event loop holds only a WEAK reference, so a task nobody refers to can
    be collected part-way through. Here that would stop the run, leave it
    non-terminal, and have a client poll forever for an answer that is never
    coming.
    """
    source = inspect.getsource(main.submit_run)
    assert "_running.add(task)" in source
    assert "add_done_callback" in source, (
        "held forever is a leak; it has to be dropped when the task finishes"
    )


def test_execution_is_bounded_and_the_rest_wait_in_QUEUED(service, monkeypatch):
    """Accepting work is cheap; doing it is not.

    anyio's default thread limiter is 40, so without a bound a burst of
    submissions became up to forty snakemake processes at once. Runs beyond the
    limit wait in QUEUED, which is precisely what WES means by it.
    """
    import threading

    # The limit itself, not a substituted semaphore. Patching in a fresh one
    # was how the loop-binding defect stayed hidden: the module-level semaphore
    # was never contended in a test, so it was never bound, so nothing noticed
    # that a second loop could not use it.
    monkeypatch.setattr(main, "MAX_CONCURRENT_RUNS", 2)
    main._slots_by_loop.clear()

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(ws, timeout_s=None, **kwargs):
        import os
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.25)
        with lock:
            live["now"] -= 1
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"the output")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", slow)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        run_ids = [_submit(client).json()["run_id"] for _ in range(8)]
        states = [_poll(client, run_id)["state"] for run_id in run_ids]

    assert states == [RunState.COMPLETE.value] * 8
    assert live["peak"] <= 2, (
        f"{live['peak']} runs executed at once against a limit of 2"
    )


def test_the_default_limit_is_well_below_the_threadpool_size():
    """The bound only means something if it is lower than what it bounds.

    run_in_threadpool draws on anyio's default limiter, which is 40. A limit set
    at or above that would be no limit at all, and the burst it exists to
    prevent would go through untouched.
    """
    import anyio.to_thread

    assert main.MAX_CONCURRENT_RUNS >= 1
    assert main.MAX_CONCURRENT_RUNS <= 16, (
        f"{main.MAX_CONCURRENT_RUNS} concurrent runs is not a meaningful bound "
        f"against a threadpool of 40; each run is a real snakemake process"
    )


def test_the_bound_survives_a_second_event_loop(service):
    """Each TestClient builds its own loop, and a server may be restarted.

    asyncio locks bind to a loop once contended. A single module-level
    semaphore therefore worked exactly until something waited on it, and then
    refused every later loop. Two contended runs in two loops is the smallest
    thing that shows it.
    """
    import threading

    monkeypatch_free_limit = 1
    main.MAX_CONCURRENT_RUNS = monkeypatch_free_limit
    main._slots_by_loop.clear()

    from fastapi.testclient import TestClient

    try:
        for _ in range(2):                        # two separate event loops
            with TestClient(main.app) as client:
                run_ids = [_submit(client).json()["run_id"] for _ in range(3)]
                states = [_poll(client, r)["state"] for r in run_ids]
            assert states == [RunState.COMPLETE.value] * 3, states
    finally:
        main.MAX_CONCURRENT_RUNS = 4
        main._slots_by_loop.clear()
