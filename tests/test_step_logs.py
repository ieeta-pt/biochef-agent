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

Snakemake's output carries `Error in rule <name>:` blocks, and the emitter
derives every rule name from the node id -- dots and dashes become underscores.
These blocks help diagnose a failed run, though a tool can imitate the heading.
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
from steplogs import clamp, failing_steps, make_step_log_names, read_node_logs

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


def _submit(client, content=b"in"):
    return client.post("/runs", data={"biochef_workflow": WORKFLOW},
                       files=[("files", ("input-1-out", content,
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
    assert body["failed_steps"] == {}, "nothing failed, so nothing is blamed"


def test_real_failing_tool_stderr_is_available_by_node(service, monkeypatch):
    """Snakemake's own failure heading comes after the tool's real message."""
    from fastapi.testclient import TestClient
    from runner import SubprocessRunner

    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    convert.client.binary_bytes = b'#!/bin/sh\ncat "$1" >&2\nexit 3\n'
    marker = b"ACTUAL_STEP_ERROR_91fa\n"

    with TestClient(main.app) as client:
        run_id = _submit(client, marker).json()["run_id"]
        assert _wait(service, run_id, TERMINAL_STATES) is RunState.EXECUTOR_ERROR
        body = client.get(f"/runs/{run_id}/logs").json()
        run = client.get(f"/runs/{run_id}").json()

    assert run["steps"]["tn93.distance-1"] == "FAILED"
    assert "tn93.distance-1" in body["failed_steps"]
    node = body["node_logs"]["tn93.distance-1"]
    assert marker.decode() in node["stderr"]
    assert node["stdout"] == ""
    assert marker.decode() not in body["stderr"]


def test_chatty_failing_tool_keeps_its_last_error_by_node(
        service, monkeypatch):
    from fastapi.testclient import TestClient
    from runner import SubprocessRunner
    import steplogs

    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    monkeypatch.setattr(steplogs, "MAX_LOG_BYTES", 128)
    convert.client.binary_bytes = (
        b'#!/bin/sh\nprintf "%01024d" 0 >&2\n'
        b'printf "LAST_ERROR\\n" >&2\nexit 3\n'
    )

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert _wait(service, run_id, TERMINAL_STATES) is RunState.EXECUTOR_ERROR
        body = client.get(f"/runs/{run_id}/logs").json()

    log = body["node_logs"]["tn93.distance-1"]["stderr"]
    assert "LAST_ERROR\n" in log
    assert "bytes omitted" in log
    assert len(log.split("\n", 1)[1].encode()) <= 128


def test_scientific_stdout_stays_an_output_not_a_log(service, monkeypatch):
    from fastapi.testclient import TestClient
    from runner import SubprocessRunner
    import base64

    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    convert.client.binary_bytes = (
        b'#!/bin/sh\nprintf "SCIENCE_DATA_28ef"\nprintf "diagnostic\\n" >&2\n'
    )

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert _wait(service, run_id, TERMINAL_STATES) is RunState.COMPLETE
        body = client.get(f"/runs/{run_id}/logs").json()
        run = client.get(f"/runs/{run_id}").json()

    node = body["node_logs"]["tn93.distance-1"]
    assert node["stdout"] == ""
    assert node["stderr"] == "diagnostic\n"
    assert "SCIENCE_DATA_28ef" not in str(body)
    assert base64.b64decode(run["outputs"]["tn93.distance-1"]["out"]) == b"SCIENCE_DATA_28ef"


@pytest.mark.parametrize("failing", [False, True], ids=["success", "failure"])
def test_parallel_rules_have_separate_stdout_and_stderr(
        service, monkeypatch, failing):
    from fastapi.testclient import TestClient
    from runner import SubprocessRunner

    monkeypatch.setattr(main, "RUNNER", SubprocessRunner())
    bundle = json.loads(convert.client.bundle_bytes)
    bundle["io"]["outputs"][0]["mode"] = "file"
    convert.client.bundle_bytes = json.dumps(bundle).encode()
    convert.client.binary_bytes = (
        b'#!/bin/sh\ncat "$1" >&2\nexit 3\n' if failing else
        b'#!/bin/sh\ncp "$1" "$2"\nprintf "stdout:%s\\n" "$1"\ncat "$1" >&2\n'
    )

    workflow = json.dumps({
        "nodes": [
            {"id": f"input-{i}", "type": "inputWorkflowNode", "data": {}}
            for i in (1, 2)
        ] + [
            {"id": f"tool-{i}", "type": "workflowNode",
             "data": {"label": "tool", "repo": "r", "paramValues": {}, "outputs": {}}}
            for i in (1, 2)
        ] + [
            {"id": f"output-{i}", "type": "outputWorkflowNode", "data": {}}
            for i in (1, 2)
        ],
        "edges": [edge for i in (1, 2) for edge in (
            {"source": f"input-{i}", "sourceHandle": "out",
             "target": f"tool-{i}", "targetHandle": "in"},
            {"source": f"tool-{i}", "sourceHandle": "out",
             "target": f"output-{i}", "targetHandle": "in"},
        )],
    })

    with TestClient(main.app) as client:
        response = client.post(
            "/runs", data={"biochef_workflow": workflow},
            files=[("files", (f"input-{i}-out", f"NODE_{i}".encode(),
                              "application/octet-stream")) for i in (1, 2)],
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        expected = RunState.EXECUTOR_ERROR if failing else RunState.COMPLETE
        assert _wait(service, run_id, TERMINAL_STATES) is expected
        logs = client.get(f"/runs/{run_id}/logs").json()["node_logs"]

    assert set(logs) == {"tool-1", "tool-2"}
    for i, other in ((1, 2), (2, 1)):
        expected_stdout = "" if failing else f"stdout:input-{i}-out\n"
        assert logs[f"tool-{i}"]["stdout"] == expected_stdout
        assert logs[f"tool-{i}"]["stderr"] == f"NODE_{i}"
        assert f"NODE_{other}" not in logs[f"tool-{i}"]["stderr"]


def test_a_successful_tool_cannot_claim_another_step_failed(service, monkeypatch):
    from fastapi.testclient import TestClient

    def noisy(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        with open(os.path.join(ws.path, "tn93.distance-1-out"), "wb") as f:
            f.write(b"result")
        return 0, "", "Error in rule tn93_distance_1:\n    jobid: 1\n"

    monkeypatch.setattr(main, "run_snakemake", noisy)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait(service, run_id, TERMINAL_STATES)
        body = client.get(f"/runs/{run_id}/logs").json()

    assert "Error in rule tn93_distance_1:" in body["stderr"]
    assert body["failed_steps"] == {}


def test_step_blocks_use_only_the_retained_stderr_tail(service, monkeypatch):
    from fastapi.testclient import TestClient
    import steplogs

    monkeypatch.setattr(steplogs, "MAX_LOG_BYTES", 120)
    heading = "Error in rule tn93_distance_1:\n"

    def failing(ws, timeout_s=None, on_start=None, on_finish=None, **kwargs):
        return 1, "", heading + ("x" * 1000) + "\n" + heading + "recent error\n"

    monkeypatch.setattr(main, "run_snakemake", failing)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        _wait(service, run_id, TERMINAL_STATES)
        body = client.get(f"/runs/{run_id}/logs").json()

    step_text = body["failed_steps"]["tn93.distance-1"]["stderr"]
    assert "recent error" in step_text
    assert "x" * 1000 not in step_text
    assert len(step_text.encode("utf-8")) <= 120


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

    assert "tn93.distance-1" in body["failed_steps"], body["failed_steps"]
    step = body["failed_steps"]["tn93.distance-1"]
    assert step["rule"] == "tn93_distance_1"
    assert "command exited with non-zero exit code" in step["stderr"]


def test_logs_are_readable_before_the_run_reaches_a_terminal_state(
        service, monkeypatch):
    """Which is a narrower claim than it first looks, and worth stating exactly.

    The logs are recorded as soon as the workflow process exits, so they can be
    read while the run is still finishing -- collecting outputs, tidying up.
    They are NOT available during execution itself: communicate() buffers until
    the process ends, so nothing exists to read while the tools are running.

    An earlier version of this test was called "while the run is still going"
    and its docstring said a fifteen-minute run should not have to end before
    anyone can look. That is not what it proves, and the README said the same
    thing. Following a run as it goes means reading the pipes incrementally,
    which this does not do.
    """
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


def test_bearer_protects_a_run_log_response(service):
    from auth import AuthenticationMiddleware, BearerAuth
    from fastapi.testclient import TestClient

    run = service.create()
    service.record_logs(run.run_id, "", "", {},
                        {"tool-1": {"stdout": "", "stderr": "private diagnostic"}})
    guarded = AuthenticationMiddleware(
        main.app, provider=BearerAuth(token="test-secret"))

    with TestClient(guarded) as client:
        url = f"/runs/{run.run_id}/logs"
        refused = client.get(url)
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == "Bearer"
        assert "private diagnostic" not in refused.text
        admitted = client.get(
            url, headers={"Authorization": "Bearer test-secret"})
        assert admitted.status_code == 200
        assert admitted.json()["node_logs"]["tool-1"]["stderr"] == "private diagnostic"


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


def test_log_limit_counts_utf8_bytes():
    clamped = clamp("éééEND", limit=6)
    assert clamped == "[... 4 earlier bytes dropped ...]\néEND"
    assert len(clamped.split("\n", 1)[1].encode("utf-8")) <= 6


def test_log_limit_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        clamp("text", limit=0)


def test_node_log_tails_share_a_bounded_budget(tmp_path, monkeypatch):
    from workspace import make_workspace
    import steplogs

    monkeypatch.setattr(steplogs, "MAX_LOG_BYTES", 12)
    ws = make_workspace(str(tmp_path))
    try:
        names = make_step_log_names(2)
        ws.write_bytes(names[0][1], (b"x" * 40) + "éEND".encode())
        ws.write_bytes(names[1][1], (b"y" * 40) + b"TAIL")

        logs = read_node_logs(ws, ["one", "two"], names)
        assert set(logs) == {"one", "two"}
        assert logs["one"]["stderr"].endswith("éEND")
        assert logs["two"]["stderr"].endswith("TAIL")
        for node in logs.values():
            assert "bytes omitted" in node["stderr"]
            assert len(node["stderr"].split("\n", 1)[1].encode()) <= 6
    finally:
        ws.cleanup()


def test_node_log_reader_refuses_a_symlink(tmp_path):
    from workspace import UnsafeName, make_workspace

    ws = make_workspace(str(tmp_path))
    secret = tmp_path / "outside.txt"
    secret.write_text("OUTSIDE_DATA")
    try:
        names = make_step_log_names(1)
        (Path(ws.path) / names[0][1]).symlink_to(secret)
        with pytest.raises(UnsafeName, match="symbolic link"):
            read_node_logs(ws, ["tool-1"], names)
    finally:
        ws.cleanup()


def test_node_log_reader_refuses_a_fifo_without_blocking(tmp_path):
    import signal
    from workspace import UnsafeName, make_workspace

    ws = make_workspace(str(tmp_path))
    try:
        names = make_step_log_names(1)
        os.mkfifo(Path(ws.path) / names[0][1])

        def timed_out(_signum, _frame):
            raise TimeoutError("opening a FIFO blocked the run")

        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, 2)
        try:
            with pytest.raises(UnsafeName, match="not a regular file"):
                read_node_logs(ws, ["tool-1"], names)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    finally:
        ws.cleanup()


def test_the_regex_is_not_the_limit_on_which_names_can_be_attributed():
    """Names it rejects are names the emitter cannot emit anyway.

    A rule name becomes a Python identifier in the generated Snakefile, so a
    node id starting with a digit, or containing a space or a plus, breaks
    snakemake's parser before any of this is reached -- confirmed against 9.21,
    which raises "invalid decimal literal" on `rule 1_tool:`.

    Pinned so nobody loosens the pattern to admit names that cannot exist, and
    concludes attribution is broken when it is the emitter that would be.
    """
    import subprocess
    import shutil

    from steplogs import _ERROR_IN_RULE

    unmatchable = [convert.rule_name_for(n) for n in ("1-tool", "a b", "tool+1")]
    for rule in unmatchable:
        assert not _ERROR_IN_RULE.search(f"Error in rule {rule}:\n")

    snakemake = shutil.which("snakemake")
    if snakemake is None:
        pytest.skip("snakemake is not on PATH")

    import tempfile
    directory = tempfile.mkdtemp()
    try:
        with open(os.path.join(directory, "Snakefile"), "w") as handle:
            handle.write('rule 1_tool:\n    output: o_0="o.txt"\n'
                         '    shell: "true"\n')
        done = subprocess.run(
            [snakemake, "-s", os.path.join(directory, "Snakefile"),
             "-d", directory, "--cores", "1", "--dry-run"],
            capture_output=True, text=True, timeout=120)
        assert done.returncode != 0, (
            "a digit-leading rule name parsed; the regex would then be the "
            "thing standing between it and attribution"
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)
