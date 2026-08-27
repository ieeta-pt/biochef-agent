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
    assert body["failed_steps"] == {}, "nothing failed, so nothing is blamed"


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


# --------------------------------------------------------------------------
# the logs while a run is happening, not only once it has ended


def test_only_the_ticker_delivers_so_the_log_cannot_go_backwards():
    """Two readers building snapshots can hand them over in the other order.

    Measured on the version that let either reader deliver: sizes arrived
    [22, 11], so a client polling twice saw LESS the second time. One
    delivering thread cannot do that.
    """
    import threading
    import time as _time

    from steplogs import LiveLog

    delivered = []
    live = LiveLog(on_flush=lambda out, err: delivered.append(len(out)),
                   every_seconds=0.05)
    live.start()
    try:
        def write(tag):
            for n in range(200):
                live.add("stdout", f"{tag}-{n:04d}\n")
                _time.sleep(0.001)

        threads = [threading.Thread(target=write, args=(t,)) for t in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        _time.sleep(0.2)
    finally:
        live.close()

    assert len(delivered) > 1, "nothing was delivered while the writers ran"
    assert delivered == sorted(delivered), (
        f"the log went backwards: {delivered}"
    )


def test_adding_a_line_never_delivers_it_itself():
    """So a reader draining the tool's pipes is never held up by a consumer.

    If the readers stall the pipe fills and the tool blocks writing to it, so a
    slow log consumer would stop the workflow.
    """
    from steplogs import LiveLog

    delivered = []
    live = LiveLog(on_flush=lambda out, err: delivered.append(out),
                   every_seconds=10 ** 9)

    for n in range(1000):
        live.add("stdout", f"line-{n}\n")

    assert delivered == [], (
        "add() delivered on its own; only the ticker should"
    )
    assert live.snapshot()[0].count("\n") == 1000


def test_a_quiet_tool_is_still_visible():
    """The case the whole timer exists for.

    A tool that prints "starting" and then works silently for ten minutes would
    show nothing for ten minutes if the clock were only checked when a line
    arrived.
    """
    import time as _time

    from steplogs import LiveLog

    seen = []
    with LiveLog(on_flush=lambda out, err: seen.append(err),
                 every_seconds=0.2) as live:
        live.add("stderr", "starting analysis\n")
        _time.sleep(0.7)

    assert seen, "a tool that printed once and then went silent showed nothing"
    assert "starting analysis" in seen[0]


def test_the_buffer_is_bounded_by_what_the_store_would_keep():
    """It held everything before: 12.4 MiB for 7.7 MiB of output, on top of the
    runner's own copy, while the store was only ever going to keep a megabyte.

    Bounding it also bounds the cost of joining, which grew with the log.
    """
    from steplogs import LiveLog

    live = LiveLog(on_flush=None, max_bytes=4096, every_seconds=10 ** 9)
    for n in range(5000):
        live.add("stdout", f"{n:06d} " + "x" * 60 + "\n")

    out, _ = live.snapshot()
    assert len(out) <= 4096 * 2, f"the buffer grew to {len(out)} bytes"
    assert "004999" in out, "the newest output was trimmed instead of the oldest"


def test_one_line_longer_than_the_whole_budget_is_still_bounded():
    """A tool that emits no newline arrives as a single enormous line.

    A progress bar redrawing with \r, or binary written to stdout, produces one
    "line" of whatever size. Refusing to trim the last line left the bound
    meaningless: 4 MiB was held against a 1 KiB cap.
    """
    from steplogs import LiveLog

    live = LiveLog(on_flush=None, max_bytes=1024, every_seconds=10 ** 9)
    live.add("stdout", "x" * (4 * 1024 * 1024))

    shown = live.snapshot()[0]
    marker, _, content = shown.partition("\n")

    assert len(content) <= 1024, f"{len(content)} bytes held against a 1024 cap"
    assert "earlier bytes dropped" in marker, (
        "the line was cut without saying so, which reads like a tool that "
        "produced half a line"
    )


def test_the_tail_of_an_oversized_line_is_what_is_kept():
    """Same reason the oldest lines go first: an error arrives at the end."""
    from steplogs import LiveLog

    live = LiveLog(on_flush=None, max_bytes=64, every_seconds=10 ** 9)
    live.add("stderr", "A" * 500 + "THE ERROR")

    assert "THE ERROR" in live.snapshot()[1]


def test_a_stream_that_is_not_one_of_the_two_is_refused():
    """It used to be accumulated into a bucket snapshot() never read.

    Held forever and shown to nobody, which is the worst of both. There are
    exactly two streams; anything else is a caller error.
    """
    import pytest as _pytest

    from steplogs import LiveLog

    live = LiveLog(on_flush=None, every_seconds=10 ** 9)
    with _pytest.raises(KeyError):
        live.add("other", "invisible\n")

    # And nothing was kept. Asserting only that it raised was too weak: the
    # version that accumulated appended the line and THEN raised on the byte
    # count, so it raised the same KeyError while still holding the content
    # forever.
    assert not any(live._lines.get("other") or ()), (
        "the line was buffered into a bucket nothing will ever read"
    )
    assert live.snapshot() == ("", "")


def test_a_failed_delivery_is_offered_again():
    """Otherwise a tool that fell quiet right after one would show nothing more.

    The content is still buffered either way; what was missing was any reason
    for the ticker to try it again before the next line arrived.
    """
    from steplogs import LiveLog

    attempts = []

    def sometimes_angry(out, err):
        attempts.append(out)
        if len(attempts) == 1:
            raise RuntimeError("the consumer is briefly broken")

    live = LiveLog(on_flush=sometimes_angry, every_seconds=10 ** 9)
    live.add("stdout", "important\n")

    try:
        live.flush()
    except RuntimeError:
        pass
    live.flush()                       # no new lines added

    assert len(attempts) == 2, (
        "the content was not offered again after a failed delivery"
    )
    assert "important" in attempts[1]


def test_trimming_drops_the_oldest_first():
    """The tail is what matters; an error arrives at the end."""
    from steplogs import LiveLog

    live = LiveLog(on_flush=None, max_bytes=100, every_seconds=10 ** 9)
    for n in range(50):
        live.add("stdout", f"line-{n:03d}\n")

    out, _ = live.snapshot()
    assert "line-049" in out
    assert "line-000" not in out


def test_both_streams_accumulate_separately():
    from steplogs import LiveLog

    live = LiveLog()
    live.add("stdout", "out\n")
    live.add("stderr", "err\n")

    out, err = live.snapshot()
    assert out == "out\n" and err == "err\n"


def test_the_live_log_survives_two_threads_writing_at_once():
    """Both reader threads call add(). It has a lock of its own for that."""
    import threading

    from steplogs import LiveLog

    live = LiveLog(every_seconds=10 ** 9, max_bytes=10 ** 9)
    barrier = threading.Barrier(2)

    def write(stream):
        barrier.wait()
        for n in range(500):
            live.add(stream, f"{stream}-{n}\n")

    threads = [threading.Thread(target=write, args=(s,))
               for s in ("stdout", "stderr")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    out, err = live.snapshot()
    assert out.count("\n") == 500, f"{out.count(chr(10))} of 500 stdout lines"
    assert err.count("\n") == 500, f"{err.count(chr(10))} of 500 stderr lines"


def test_close_waits_for_a_flush_that_is_already_running():
    """Otherwise a partial log can land after the complete one.

    perform_run closes the live log and then records the authoritative output.
    If close() only asked the ticker to stop and did not wait, a flush already
    in progress could deliver its partial snapshot after that.
    """
    import threading
    import time as _time

    from steplogs import LiveLog

    in_flush = threading.Event()
    finished_flush = threading.Event()

    def slow_flush(out, err):
        in_flush.set()
        _time.sleep(0.3)
        finished_flush.set()

    live = LiveLog(on_flush=slow_flush, every_seconds=0.05)
    live.start()
    try:
        live.add("stdout", "something\n")
        assert in_flush.wait(5), "the ticker never flushed"
        live.close()
        assert finished_flush.is_set(), (
            "close() returned while a flush was still running; its partial "
            "snapshot can land after the authoritative record"
        )
    finally:
        live.close()


def test_the_flush_callback_is_not_called_holding_the_lock():
    """A slow consumer must not block anything that appends."""
    from steplogs import LiveLog

    held = []

    def inspect_lock(out, err):
        acquired = live._lock.acquire(blocking=False)
        held.append(not acquired)
        if acquired:
            live._lock.release()

    live = LiveLog(on_flush=inspect_lock, every_seconds=10 ** 9)
    live.add("stdout", "a line\n")
    live.flush()

    assert held == [False], "the buffer lock was held while calling out"


def test_logs_are_readable_while_the_tools_are_still_running(service,
                                                             monkeypatch):
    """What the README used to say was impossible.

    Verified against real snakemake as well: a 4.34s two-step workflow flushed
    at +0.87s, +1.98s and +3.12s, with the visible output growing each time.
    """
    import threading

    from fastapi.testclient import TestClient

    printed = threading.Event()
    release = threading.Event()

    def chatty(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        on_line("stderr", "rule step_one:\n")
        on_line("stdout", "the tool is talking\n")
        printed.set()
        release.wait(15)
        with open(os.path.join(ws.path, "tn93.distance-1-out"), "wb") as f:
            f.write(b"done")
        return 0, "", "everything is fine\n"

    monkeypatch.setattr(main, "run_snakemake", chatty)

    with TestClient(main.app) as client:
        run_id = _submit(client).json()["run_id"]
        assert printed.wait(15), "the stub never ran"

        # Give the flush a moment; it is batched, not synchronous.
        deadline = time.time() + 10
        body = {}
        while time.time() < deadline:
            body = client.get(f"/runs/{run_id}/logs").json()
            if body.get("stdout"):
                break
            time.sleep(0.05)

        assert body["state"] not in {s.value for s in TERMINAL_STATES}, (
            "the run had already finished; this test proved nothing"
        )
        assert "the tool is talking" in body["stdout"], body

        release.set()
        _wait(service, run_id, TERMINAL_STATES)
        final = client.get(f"/runs/{run_id}/logs").json()

    # The authoritative record replaces the partial one.
    assert final["stderr"] == "everything is fine\n", final["stderr"]


def test_a_caller_wanting_logs_but_not_progress_still_gets_them():
    """on_line used to be wired only when progress was wanted, so asking for
    live logs alone got neither."""
    import inspect

    source = inspect.getsource(main.perform_run)
    assert "on_progress is not None or on_logs is not None" in source
    assert "on_line=observe if on_progress else None" not in source


def test_a_run_does_not_leave_its_ticker_thread_behind(service, monkeypatch):
    """One thread per run, and runs are the thing this service does most.

    Verified by hand at first, which is not the same as covered: removing the
    close() passed the whole suite. A service that leaks a thread per run
    degrades slowly and blames the wrong thing.
    """
    import threading
    import time as _time

    from fastapi.testclient import TestClient

    def quick(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        on_line("stdout", "a line\n")
        with open(os.path.join(ws.path, "tn93.distance-1-out"), "wb") as f:
            f.write(b"done")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", quick)

    before = threading.active_count()
    with TestClient(main.app) as client:
        for _ in range(12):
            run_id = _submit(client).json()["run_id"]
            _wait(service, run_id, TERMINAL_STATES)

    # The tickers wake on an interval, so give any survivor time to be counted.
    _time.sleep(1.0)
    leaked = threading.active_count() - before

    assert leaked <= 1, (
        f"{leaked} threads outlived 12 runs; the ticker is not being stopped"
    )


def test_the_ticker_is_stopped_even_when_the_run_fails(service, monkeypatch):
    """The failure path is where a finally earns its keep."""
    import threading
    import time as _time

    from fastapi.testclient import TestClient

    def explodes(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        on_line("stderr", "about to fail\n")
        raise RuntimeError("the runner broke")

    monkeypatch.setattr(main, "run_snakemake", explodes)

    before = threading.active_count()
    with TestClient(main.app) as client:
        for _ in range(12):
            run_id = _submit(client).json()["run_id"]
            _wait(service, run_id, TERMINAL_STATES)

    _time.sleep(1.0)
    leaked = threading.active_count() - before

    assert leaked <= 1, (
        f"{leaked} threads outlived 12 failing runs"
    )
