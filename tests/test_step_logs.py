"""What a client can learn about what a run actually printed (#6).

Before this, almost nothing.

The runner captures stdout and stderr separately. The handler unpacks both and
throws stdout away -- the variable is literally named `_out` -- then keeps the
last 2000 characters of stderr, and only when the run failed. A run that
succeeded reports nothing at all, and a run that failed reports a tail that may
begin mid-word and may not include the error that mattered.

Nothing attributes any of it to a step. A workflow is a graph of tools; when one
of them fails, the question is always which, and the answer is somewhere in a
truncated string or nowhere.

Snakemake does say. Its output carries `Error in rule <name>:` blocks, and the
emitter derives every rule name from the node id -- dots and dashes become
underscores. So the attribution is available and simply not used.
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
import os
import time

import pytest

import convert
from runs import RunState, RunStore
from steplogs import clamp, failing_steps

BUNDLE = {"id": "tool", "name": "tool", "bin": "tool",
          "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
                 "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
          "parameters": []}

WORKFLOW = json.dumps({
    "nodes": [
        {"id": "input-1", "type": "inputWorkflowNode", "data": {}},
        {"id": "tn93.distance-1", "type": "workflowNode",
         "data": {"label": "tool", "repo": "r", "paramValues": {}, "outputs": {}}},
        {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
    ],
    "edges": [
        {"source": "input-1", "sourceHandle": "out",
         "target": "tn93.distance-1", "targetHandle": "in"},
        {"source": "tn93.distance-1", "sourceHandle": "out",
         "target": "output-1", "targetHandle": "in"},
    ],
})

# The shape snakemake actually produces, checked against 9.21.
SNAKEMAKE_FAILURE = """Building DAG of jobs...
[Mon Aug 24 12:00:00 2026]
rule tn93_distance_1:
    input: input-1-out
    output: tn93.distance-1-out
Error in rule tn93_distance_1:
    jobid: 1
    input: input-1-out
    output: tn93.distance-1-out
    shell:
        ./tool < input-1-out > tn93.distance-1-out
        (command exited with non-zero exit code)
Shutting down, this might take some time.
"""


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


def _wait(store, run_id, states, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if store.get(run_id).state in states:
            break
        time.sleep(0.02)
    return store.get(run_id).state


TERMINAL_STATES = {RunState.COMPLETE, RunState.EXECUTOR_ERROR,
                   RunState.SYSTEM_ERROR, RunState.CANCELED}


# --------------------------------------------------------------------------
# the endpoint


def test_a_successful_run_reports_what_it_printed(service, monkeypatch):
    """Not only failures. A tool that warns is worth reading."""
    from fastapi.testclient import TestClient

    def noisy(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        with open(os.path.join(ws.path, "tn93.distance-1-out"), "wb") as f:
            f.write(b"result")
        return 0, "progress on stdout", "a warning on stderr"

    monkeypatch.setattr(main, "run_snakemake", noisy)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait(service, run_id, TERMINAL_STATES)
        body = client.get(f"/runs/{run_id}/logs").json()

    assert body["stdout"] == "progress on stdout"
    assert body["stderr"] == "a warning on stderr"
    assert body["steps"] == {}, "nothing failed, so nothing is blamed"


def test_a_failing_run_keeps_its_whole_stderr(service, monkeypatch):
    """Not a 2000-character tail.

    The old failure detail kept err[-2000:], which can begin mid-word and can
    drop the error that mattered when a tool is chatty before it dies.
    """
    from fastapi.testclient import TestClient

    long_error = ("noise\n" * 5000) + "THE ACTUAL ERROR\n"

    def failing(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        return 1, "", long_error

    monkeypatch.setattr(main, "run_snakemake", failing)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait(service, run_id, TERMINAL_STATES)
        body = client.get(f"/runs/{run_id}/logs").json()

    assert "THE ACTUAL ERROR" in body["stderr"]
    assert len(body["stderr"]) > 2000, "still truncated to the old tail"


def test_a_failing_step_is_named(service, monkeypatch):
    """Which step broke is the question a graph of tools always raises."""
    from fastapi.testclient import TestClient

    def failing(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        return 1, "", SNAKEMAKE_FAILURE

    monkeypatch.setattr(main, "run_snakemake", failing)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait(service, run_id, TERMINAL_STATES)
        body = client.get(f"/runs/{run_id}/logs").json()

    assert "tn93.distance-1" in body["steps"], body["steps"]
    step = body["steps"]["tn93.distance-1"]
    assert step["rule"] == "tn93_distance_1"
    assert "command exited with non-zero exit code" in step["stderr"]


def test_logs_are_readable_while_the_run_is_still_going(service, monkeypatch):
    """A fifteen-minute run should not have to end before anyone can look."""
    import threading

    from fastapi.testclient import TestClient

    recorded = threading.Event()
    release = threading.Event()

    def slow(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        # run_snakemake returns, logs get recorded, then the run keeps working.
        return 0, "said something early", ""

    def stall(*a, **k):
        recorded.set()
        release.wait(15)

    monkeypatch.setattr(main, "run_snakemake", slow)
    original = main.RUNS.record_logs

    def record_then_stall(*a, **k):
        original(*a, **k)
        stall()

    monkeypatch.setattr(main.RUNS, "record_logs", record_then_stall)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert recorded.wait(15)
        body = client.get(f"/runs/{run_id}/logs").json()
        assert body["state"] not in {s.value for s in TERMINAL_STATES}, (
            "the run had already finished; this test proved nothing"
        )
        assert body["stdout"] == "said something early"
        release.set()
        _wait(service, run_id, TERMINAL_STATES)


def test_logs_for_an_unknown_run_are_404(service):
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        assert client.get("/runs/nope/logs").status_code == 404


# --------------------------------------------------------------------------
# attribution, which is the part that can be quietly wrong


def test_the_rule_name_comes_from_the_emitter_itself():
    """One transform, not two.

    A second copy would work until someone changed the emitter, and then
    attribute failures to nothing at all.
    """
    import inspect

    assert "rule_name_for(node.id)" in inspect.getsource(convert.convert_to_snakemake)
    assert convert.rule_name_for("tn93.distance-1") == "tn93_distance_1"


def test_a_rule_this_workflow_did_not_produce_is_not_attributed():
    """snakemake reports on rules of its own, "all" among them."""
    attributed = failing_steps("Error in rule all:\n    jobid: 0\n",
                               ["tool-1"], convert.rule_name_for)
    assert attributed == {}


def test_two_nodes_that_collide_are_both_reported_as_ambiguous():
    """"a.b" and "a-b" both become "a_b".

    Picking one would put a failure against a step that did not have it, which
    is worse than saying the attribution is uncertain.
    """
    attributed = failing_steps("Error in rule a_b:\n    jobid: 1\n",
                               ["a.b", "a-b"], convert.rule_name_for)

    assert set(attributed) == {"a.b", "a-b"}
    assert attributed["a.b"]["ambiguous"] == ["a-b", "a.b"]


def test_several_failing_steps_are_each_named():
    stderr = ("Error in rule step_one:\n    jobid: 1\n"
              "Error in rule step_two:\n    jobid: 2\n")
    attributed = failing_steps(stderr, ["step-one", "step-two"],
                               convert.rule_name_for)

    assert set(attributed) == {"step-one", "step-two"}
    assert "jobid: 1" in attributed["step-one"]["stderr"]
    assert "jobid: 2" in attributed["step-two"]["stderr"]
    assert "jobid: 2" not in attributed["step-one"]["stderr"], (
        "one step's block ran into the next"
    )


def test_output_larger_than_the_cap_is_cut_at_the_front_and_says_so():
    """The tail is what matters, and a silent cut reads like a broken tool."""
    text = ("x" * 100) + "THE END"
    clamped = clamp(text, limit=20)

    assert clamped.endswith("THE END")
    assert len(clamped) <= 20 + 60
    assert "earlier bytes dropped" in clamped


def test_output_within_the_cap_is_untouched():
    assert clamp("short", limit=1000) == "short"
