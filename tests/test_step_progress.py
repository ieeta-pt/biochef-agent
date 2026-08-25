"""What the editor could paint on a node while a run is happening (#8).

Recorded before anything changes. Nothing, until the run is over.

B4 wants per-step state -- pending, running, done, failed -- good enough to
colour a node. Two things stand in the way, and only one of them is obvious.

The obvious one: no step has a state. `steps` is populated once, from the
finished stderr, and only ever with the steps that FAILED. A step that is
running, or waiting, or finished successfully, is indistinguishable from a step
that does not exist.

The other one is why the first cannot simply be fixed in the store. The runner
captures output with communicate(), which buffers until the workflow process
exits. So there is nothing to read while the tools are running -- not a little,
none -- and any per-step state derived from that output could only appear after
the entire run had finished, which is exactly when nobody needs it painted.

Snakemake does report as it goes. Confirmed against 9.21, it prints

    localrule step_one:
    Finished jobid: 2 (Rule: step_one)
    1 of 3 steps (33%) done

so the transitions are all there, arriving in real time, into a pipe nobody
reads until the end.
"""

import inspect
import os
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
from runs import RunStore


def test_a_run_reports_no_per_step_state():
    """Only outputs and an overall state; nothing about individual nodes."""
    store = RunStore()
    run = store.create()
    assert set(run.as_dict()) == {"run_id", "state"}


def test_the_only_per_step_information_is_which_ones_failed():
    """So pending, running and done are all the same thing: absent."""
    import convert
    from steplogs import failing_steps

    # A workflow where one step failed and one succeeded. Only the failure is
    # described; the successful step is absent, which is the same as pending,
    # which is the same as never having existed.
    stderr = "Error in rule step_one:\n    jobid: 1\n"
    attributed = failing_steps(stderr, ["step-one", "step-two"],
                               convert.rule_name_for)

    assert set(attributed) == {"step-one"}
    assert "step-two" not in attributed, (
        "a step that succeeded is indistinguishable from one that never ran"
    )
    assert "status" not in attributed["step-one"], (
        "even the failing step carries no status field, only its error text"
    )


def test_the_output_is_read_as_it_arrives():
    """Which is what makes per-step state possible at all.

    communicate() returns only when the process ends, so while it was used
    nothing could be reported about a run in progress. Both streams are drained
    by their own reader now -- both, because draining one and not the other is
    the deadlock communicate() exists to avoid.
    """
    source = inspect.getsource(runner_module.Runner.run)
    assert "process.communicate(timeout=timeout_s)" not in source
    assert "for line in stream" in source
    assert source.count("threading.Thread") == 2, (
        "both pipes must have a reader, or a tool that fills the other one's "
        "buffer blocks forever"
    )


# --------------------------------------------------------------------------
# what snakemake says, and what it turns into


def _tracker(node_ids=("step-one", "step-two")):
    import convert
    from steplogs import Progress

    return Progress(list(node_ids), convert.rule_name_for)


def test_a_step_moves_from_pending_to_running_to_complete():
    from steplogs import COMPLETE, PENDING, RUNNING

    progress = _tracker()
    assert progress.snapshot() == {"step-one": PENDING, "step-two": PENDING}

    progress.observe("localrule step_one:\n")
    assert progress.snapshot()["step-one"] == RUNNING

    progress.observe("Finished jobid: 2 (Rule: step_one)\n")
    assert progress.snapshot()["step-one"] == COMPLETE
    assert progress.snapshot()["step-two"] == PENDING, (
        "a step nobody has mentioned is pending, not running"
    )


def test_both_spellings_of_a_starting_rule_are_understood():
    """snakemake writes "rule X:" or "localrule X:" depending on the executor."""
    from steplogs import RUNNING

    for spelling in ("rule step_one:", "localrule step_one:"):
        progress = _tracker()
        progress.observe(spelling + "\n")
        assert progress.snapshot()["step-one"] == RUNNING, spelling


def test_an_error_line_is_not_mistaken_for_a_step_starting():
    """"Error in rule X:" contains "rule X:".

    Matched at the start of the line for exactly this reason. Anchoring it
    anywhere else would mark a step that had just failed as freshly running,
    and the node would go green in front of someone watching it break.
    """
    from steplogs import FAILED

    progress = _tracker()
    progress.observe("Error in rule step_one:\n")
    assert progress.snapshot()["step-one"] == FAILED


def test_a_failure_is_not_overwritten_by_a_later_start():
    """A retried rule must not lose the fact that it broke."""
    from steplogs import FAILED

    progress = _tracker()
    progress.observe("Error in rule step_one:\n")
    progress.observe("localrule step_one:\n")
    assert progress.snapshot()["step-one"] == FAILED


def test_colliding_rule_names_move_together():
    """"a.b" and "a-b" both become "a_b", and marking one would be a guess."""
    from steplogs import RUNNING

    progress = _tracker(["a.b", "a-b"])
    progress.observe("rule a_b:\n")
    assert progress.snapshot() == {"a.b": RUNNING, "a-b": RUNNING}


def test_lines_that_mean_nothing_change_nothing():
    progress = _tracker()
    for line in ("Building DAG of jobs...\n", "1 of 3 steps (33%) done\n",
                 "        shell: ./tool < in > out\n", "\n"):
        assert progress.observe(line) is False, line


# --------------------------------------------------------------------------
# through the service, while the run is still going


def test_a_polled_run_carries_per_step_status_before_it_finishes(tmp_path,
                                                                 monkeypatch):
    """Which is the whole point: the editor paints nodes during the run.

    Driven through run_snakemake's on_line, exactly as the real runner feeds it.
    """
    import json
    import threading
    import time

    import convert
    from fastapi.testclient import TestClient
    from runs import RunState

    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "RUNS", RunStore())
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    class _Registry:
        def get_container(self, target):
            return target

        def get_manifest(self, container, *a, **k):
            import hashlib
            bundle = json.dumps({
                "id": "tool", "name": "tool", "bin": "tool",
                "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
                       "outputs": [{"name": "out", "types": ["T"],
                                    "mode": "stdout"}]},
                "parameters": []}).encode()
            binary = b"#!/bin/sh\n"
            self._bundle, self._binary = bundle, binary
            digest = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()
            return {"layers": [
                {"digest": digest(bundle), "mediaType": "application/octet-stream",
                 "annotations": {"org.opencontainers.image.title": "bundle.json"}},
                {"digest": digest(binary), "mediaType": "application/octet-stream",
                 "annotations": {"org.opencontainers.image.title": "tool"}}]}

        def pull(self, target, outdir):
            self.get_manifest(None)
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, "bundle.json"), "wb") as f:
                f.write(self._bundle)
            with open(os.path.join(outdir, "tool"), "wb") as f:
                f.write(self._binary)

    monkeypatch.setattr(convert, "client", _Registry())
    convert.tools.clear()

    started = threading.Event()
    release = threading.Event()

    def emitting(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        on_line("stderr", "localrule tool_1:\n")
        started.set()
        release.wait(15)
        on_line("stderr", "Finished jobid: 1 (Rule: tool_1)\n")
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as f:
            f.write(b"done")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", emitting)

    workflow = json.dumps({
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode", "data": {}},
            {"id": "tool-1", "type": "workflowNode",
             "data": {"label": "tool", "repo": "r", "paramValues": {},
                      "outputs": {}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}}],
        "edges": [
            {"source": "input-1", "sourceHandle": "out", "target": "tool-1",
             "targetHandle": "in"},
            {"source": "tool-1", "sourceHandle": "out", "target": "output-1",
             "targetHandle": "in"}]})

    try:
        with TestClient(main.app) as client:
            run_id = client.post("/runs", data={"biochef_workflow": workflow},
                                 files=[("files", ("input-1-out", b"in",
                                                   "application/octet-stream"))]
                                 ).json()["run_id"]
            if not started.wait(15):
                run = main.RUNS.get(run_id)
                raise AssertionError(
                    f"run_snakemake was never reached; the run is "
                    f"{run.state.value} with error {run.error!r}"
                )

            body = client.get(f"/runs/{run_id}").json()
            assert body["state"] == RunState.RUNNING.value, body
            assert body["steps"]["tool-1"] == "RUNNING", body

            release.set()
            deadline = time.time() + 20
            while time.time() < deadline:
                body = client.get(f"/runs/{run_id}").json()
                if body["state"] == RunState.COMPLETE.value:
                    break
                time.sleep(0.02)

        assert body["steps"]["tool-1"] == "COMPLETE", body
    finally:
        convert.tools.clear()
