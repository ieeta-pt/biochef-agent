"""What a large file costs to get in or out (#13).

Before this, several copies of itself, in memory, on both sides.

D2's acceptance is that a multi-gigabyte BAM crosses browser to agent and back
without being held fully in memory on either side. Today it cannot cross at all
on a machine of ordinary size, and the arithmetic is not subtle.

Outbound, an output is read whole, base64-encoded whole -- which is another copy,
4/3 the size -- and put in a dict that FastAPI then serialises whole. Measured on
an 8 MiB output: 8.0 MiB raw, 10.7 MiB encoded, both alive together, and the
serialised response after that. Extrapolated:

    1 GiB output  ->  ~3.7 GiB resident
    4 GiB output  ->  ~14.7 GiB resident

Inbound is simpler and no better: every part is read with `await f.read()`, so
the whole upload exists as one bytes object before the run starts.

Base64 is the deeper problem. It is in the response contract -- the editor
decodes it -- so an output cannot be streamed while it is also being encoded
into a JSON string field. Something has to give, and it should be the transport
rather than the correctness of the run.
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


import asyncio
import hashlib
import io
import json
import os
import tempfile
import threading
import time

import pytest

import convert
from datasource import (DataSourceError, HandedOverSource, SpooledSource,
                        get_sources)
from retention import Retained
from runs import RunState, RunStore
from workspace import make_workspace


# --------------------------------------------------------------------------
# the contract that has not changed


def test_the_encoded_response_is_still_what_convert_returns():
    """The editor decodes base64. That is not moving.

    An output larger than memory cannot come back this way, which is why the
    streaming endpoint exists -- not because this one was wrong.
    """
    source = inspect.getsource(main.perform_run)
    assert "base64.b64encode(raw)" in source
    assert "return results" in source


# --------------------------------------------------------------------------
# inbound: an upload never exists whole


def test_the_spooled_source_copies_without_reading_the_whole_thing(tmp_path):
    """Starlette already wrote a large part to disk; await f.read() undid that."""
    largest = {"chunk": 0}

    class _Watching(io.RawIOBase):
        def __init__(self, data):
            self._inner = io.BytesIO(data)

        def read(self, size=-1):
            data = self._inner.read(size)
            largest["chunk"] = max(largest["chunk"], len(data))
            return data

    ws = make_workspace(str(tmp_path))
    try:
        SpooledSource().fetch(ws, "input-1-out", _Watching(b"y" * (4 << 20)))
        with ws.open_read("input-1-out") as handle:
            assert len(handle.read()) == 4 << 20
    finally:
        ws.cleanup()

    assert largest["chunk"] <= 1 << 20, (
        f"read {largest['chunk']} bytes at once; the upload was not streamed"
    )


def test_the_handed_over_source_deletes_the_file_it_was_given(tmp_path):
    """One temporary file leaked per input would be a slower version of the
    problem D2 exists to fix."""
    handed = tmp_path / "spooled-by-the-service"
    handed.write_bytes(b"contents")

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        HandedOverSource().fetch(ws, "input-1-out", str(handed))
        with ws.open_read("input-1-out") as file:
            assert file.read() == b"contents"
    finally:
        ws.cleanup()

    assert not handed.exists(), "the temporary file outlived the run"


def test_the_handed_over_source_deletes_it_even_when_the_write_fails(tmp_path):
    """The failure path is the one that leaks if nobody looks."""
    handed = tmp_path / "spooled"
    handed.write_bytes(b"contents")

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        ws.write_bytes("input-1-out", b"already there")
        with pytest.raises(FileExistsError):
            HandedOverSource().fetch(ws, "input-1-out", str(handed))
    finally:
        ws.cleanup()

    assert not handed.exists(), "a failed hand-over left its file behind"


def test_neither_handler_reads_an_upload_whole():
    submit = inspect.getsource(main.submit_run)
    convert_handler = inspect.getsource(main.convert)

    assert "f.file" in convert_handler, (
        "the synchronous path should hand over starlette's spooled file"
    )
    assert "await f.read(1024 * 1024)" in submit, (
        "the asynchronous path should copy in chunks, since the request ends "
        "before the work starts"
    )
    assert "await f.read()" not in submit.replace("await f.read(1024 * 1024)", "")


# --------------------------------------------------------------------------
# outbound: an output is streamed, and survives long enough to be fetched


def _service(tmp_path, monkeypatch, output=b"the output"):
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "RUNS", RunStore())
    monkeypatch.setattr(main, "RETAINED", Retained())
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    bundle = json.dumps({
        "id": "tool", "name": "tool", "bin": "tool",
        "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
               "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
        "parameters": []}).encode()
    binary = b"#!/bin/sh\n"
    digest = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()

    class _Registry:
        def get_container(self, target):
            return target

        def get_manifest(self, container, *a, **k):
            return {"layers": [
                {"digest": digest(bundle), "mediaType": "application/octet-stream",
                 "annotations": {"org.opencontainers.image.title": "bundle.json"}},
                {"digest": digest(binary), "mediaType": "application/octet-stream",
                 "annotations": {"org.opencontainers.image.title": "tool"}}]}

        def pull(self, target, outdir):
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, "bundle.json"), "wb") as f:
                f.write(bundle)
            with open(os.path.join(outdir, "tool"), "wb") as f:
                f.write(binary)

    monkeypatch.setattr(convert, "client", _Registry())
    convert.tools.clear()

    def produce(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as handle:
            handle.write(output)
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", produce)

    return json.dumps({
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


def _finish(client, workflow, store):
    import time as _time

    run_id = client.post("/runs", data={"biochef_workflow": workflow},
                         files=[("files", ("input-1-out", b"in",
                                           "application/octet-stream"))]
                         ).json()["run_id"]
    deadline = _time.time() + 20
    while _time.time() < deadline:
        if store.get(run_id).state is RunState.COMPLETE:
            break
        _time.sleep(0.02)
    return run_id


def test_an_output_can_be_streamed_after_the_run(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    workflow = _service(tmp_path, monkeypatch, output=b"streamed bytes")
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")

        assert response.status_code == 200, response.text
        assert response.content == b"streamed bytes"
        assert response.headers["content-type"] == "application/octet-stream"
        assert "attachment" in response.headers.get("content-disposition", "")
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_a_streamed_output_is_not_base64(tmp_path, monkeypatch):
    """The whole point. Encoding costs a third again and a second copy."""
    import base64

    from fastapi.testclient import TestClient

    raw = bytes(range(256)) * 64
    workflow = _service(tmp_path, monkeypatch, output=raw)
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")

        assert response.content == raw
        assert response.content != base64.b64encode(raw)
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_a_client_cannot_name_a_path(tmp_path, monkeypatch):
    """A node and a handle, resolved against what the run recorded."""
    from fastapi.testclient import TestClient

    workflow = _service(tmp_path, monkeypatch)
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            for hostile in ("../../etc/passwd", "Snakefile", "tool"):
                response = client.get(
                    f"/runs/{run_id}/outputs/tool-1/{hostile}")
                assert response.status_code == 404, (
                    f"{hostile!r} was served: {response.status_code}")
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_an_expired_run_says_so_rather_than_404(tmp_path, monkeypatch):
    """410 Gone, because it existed and no longer does -- which is a different
    thing from never having existed, and the client can tell them apart."""
    from fastapi.testclient import TestClient

    workflow = _service(tmp_path, monkeypatch)
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            main.RETAINED.release(run_id)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")

        assert response.status_code == 410, response.text
        assert "outputs_expired" in response.text
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_malformed_async_submission_removes_owned_spool_file(
        tmp_path, monkeypatch):
    """A workflow rejected before input fetch still owns its request spool."""
    from fastapi.testclient import TestClient

    real_named_temporary_file = tempfile.NamedTemporaryFile
    handed_over = []

    def named_temporary_file(*args, **kwargs):
        kwargs["dir"] = tmp_path
        handle = real_named_temporary_file(*args, **kwargs)
        handed_over.append(handle.name)
        return handle

    monkeypatch.setattr(main, "RUNS", RunStore())
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "RETAINED", Retained())
    try:
        with TestClient(main.app) as client:
            monkeypatch.setattr(main.tempfile, "NamedTemporaryFile",
                                named_temporary_file)
            response = client.post(
                "/runs", data={"biochef_workflow": "{"},
                files=[("files", ("input-1-out", b"payload", "text/plain"))],
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]

            deadline = time.time() + 5
            while time.time() < deadline:
                status = main.RUNS.get(run_id).state.value
                if main.RUNS.get(run_id).state in main.TERMINAL:
                    break
                time.sleep(0.01)

        assert status == "SYSTEM_ERROR"
        assert handed_over and all(not os.path.exists(path) for path in handed_over)
    finally:
        main.RETAINED.release_all()


def test_queued_cancel_skips_input_fetch_and_cleans_only_owned_file(
        tmp_path, monkeypatch):
    """Cancel while waiting for a worker slot; localpath inputs remain caller-owned."""
    monkeypatch.setattr(main, "RUNS", RunStore())
    run = main.RUNS.create()
    handed = tmp_path / "handed-over"
    handed.write_bytes(b"temporary")
    original = tmp_path / "original-input"
    original.write_bytes(b"keep")
    entered = asyncio.Event()
    release_slot = asyncio.Event()

    class _HeldSlot:
        async def __aenter__(self):
            entered.set()
            await release_slot.wait()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(main, "_slots", lambda: _HeldSlot())

    async def exercise():
        task = asyncio.create_task(main._execute(
            run.run_id, "not parsed while queued", [
                ("handedover", "input-1-out", str(handed)),
                ("localpath", "input-1-out", str(original)),
            ]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            main.RUNS.advance(run.run_id, RunState.CANCELING)
        finally:
            release_slot.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())

    assert main.RUNS.get(run.run_id).state is RunState.CANCELED
    assert not handed.exists(), "queued cancellation leaked its owned spool file"
    assert original.read_bytes() == b"keep", "cleanup deleted a caller-owned input"


def test_cancel_during_output_finalization_denies_output_and_releases_workspace(
        tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    workflow = _service(tmp_path, monkeypatch, output=b"must not be released")
    original_record_outputs = main.RUNS.record_outputs

    def finish_with_diagnostics(ws, **kwargs):
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as output:
            output.write(b"must not be released")
        return 0, "diagnostic stdout", "diagnostic stderr"

    monkeypatch.setattr(main, "run_snakemake", finish_with_diagnostics)

    def record_then_cancel(run_id, catalogue):
        original_record_outputs(run_id, catalogue)
        main.RUNS.advance(run_id, RunState.CANCELING)

    monkeypatch.setattr(main.RUNS, "record_outputs", record_then_cancel)
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/runs", data={"biochef_workflow": workflow},
                files=[("files", ("input-1-out", b"in",
                                  "application/octet-stream"))],
            )
            run_id = response.json()["run_id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                run = main.RUNS.get(run_id)
                if run.state is RunState.CANCELED:
                    break
                time.sleep(0.01)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")
            logs = client.get(f"/runs/{run_id}/logs").json()

        assert run.state is RunState.CANCELED
        assert response.status_code == 409
        assert response.json()["detail"]["state"] == "CANCELED"
        assert main.RETAINED.workspace(run_id) is None
        assert logs["run_id"] == run_id
        assert logs["stdout"] == "diagnostic stdout"
        assert logs["stderr"] == "diagnostic stderr"
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_retained_file_open_finishes_before_eviction_closes_workspace(
        tmp_path):
    """Acquiring a descriptor under retention's lock survives subsequent eviction."""
    retained = Retained(keep_seconds=3600, max_retained=1)
    ws = make_workspace(str(tmp_path))
    ws.write_bytes("out", b"readable after eviction")
    retained.keep("first", ws, now=1000)

    entered_open = threading.Event()
    permit_open = threading.Event()
    eviction_started = threading.Event()
    eviction_done = threading.Event()
    real_open_read = ws.open_read

    def paused_open_read(name, **kwargs):
        entered_open.set()
        assert permit_open.wait(5), "test did not release output acquisition"
        return real_open_read(name, **kwargs)

    ws.open_read = paused_open_read
    acquired = {}

    def open_output():
        try:
            acquired["file"] = retained.open_read("first", "out", now=1001)
        except BaseException as failure:
            acquired["error"] = failure

    def evict_output():
        eviction_started.set()
        other = _FakeWorkspace(tmp_path / "second")
        try:
            retained.keep("second", other, now=1002)
        except BaseException as failure:
            acquired["eviction_error"] = failure
        finally:
            eviction_done.set()

    opener = threading.Thread(target=open_output)
    evictor = threading.Thread(target=evict_output)
    opener.start()
    try:
        assert entered_open.wait(5)
        evictor.start()
        assert eviction_started.wait(5)
        assert not eviction_done.wait(0.05), "eviction closed workspace during open"
    finally:
        permit_open.set()
        opener.join(5)
        if evictor.ident is not None:
            evictor.join(5)

    try:
        assert not opener.is_alive() and not evictor.is_alive()
        assert eviction_done.is_set()
        assert "error" not in acquired, repr(acquired.get("error"))
        assert "eviction_error" not in acquired, repr(acquired.get("eviction_error"))
        assert acquired["file"] is not None
        assert acquired["file"].read() == b"readable after eviction"
        assert retained.workspace("first", now=1003) is None
    finally:
        if "file" in acquired and acquired["file"] is not None:
            acquired["file"].close()
        retained.release_all()


# --------------------------------------------------------------------------
# retention, because "stop deleting" is how a service fills a disk


class _FakeWorkspace:
    def __init__(self, path):
        self.path = str(path)
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


def test_a_kept_workspace_is_released_when_its_time_is_up(tmp_path):
    retained = Retained(keep_seconds=60, max_retained=8)
    ws = _FakeWorkspace(tmp_path)

    assert retained.keep("run-1", ws, now=1000)
    assert retained.workspace("run-1", now=1030) is ws
    assert retained.workspace("run-1", now=1061) is None
    assert ws.cleaned, "the workspace expired but was left on disk"


def test_only_so_many_runs_keep_anything_at_once(tmp_path):
    """The time bound alone is not enough.

    A hundred runs in a minute would each honour their hour simultaneously, and
    the disk does not care that every one was individually reasonable.
    """
    retained = Retained(keep_seconds=3600, max_retained=2)
    kept = [_FakeWorkspace(tmp_path / str(n)) for n in range(4)]

    for index, ws in enumerate(kept):
        retained.keep(f"run-{index}", ws, now=1000 + index)

    assert retained.workspace("run-0", now=1010) is None
    assert retained.workspace("run-1", now=1010) is None
    assert retained.workspace("run-3", now=1010) is kept[3]
    assert kept[0].cleaned and kept[1].cleaned, (
        "evicted workspaces were forgotten but not removed"
    )


def test_retention_can_be_switched_off_and_says_so(tmp_path):
    """The caller cleans up itself then, rather than this holding nothing and
    leaving the directory behind."""
    ws = _FakeWorkspace(tmp_path)

    assert Retained(keep_seconds=0, max_retained=8).keep("run-1", ws) is False
    assert Retained(keep_seconds=60, max_retained=0).keep("run-1", ws) is False
    assert not ws.cleaned, "declining to keep it must not also delete it"


def test_a_run_whose_workspace_is_not_kept_is_cleaned_up_immediately(tmp_path,
                                                                     monkeypatch):
    """With retention off, the old behaviour exactly: gone when the run ends."""
    from fastapi.testclient import TestClient

    workflow = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "RETAINED", Retained(keep_seconds=0))
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")

        assert response.status_code == 410
        runs_root = tmp_path / "runs"
        leftover = list(runs_root.glob("biochef-run-*")) if runs_root.exists() else []
        assert not leftover, f"workspaces were left behind: {leftover}"
    finally:
        convert.tools.clear()


def test_a_large_output_arrives_intact(tmp_path, monkeypatch):
    """D2's acceptance, as far as an in-process client can show it.

    Not the chunking. TestClient runs the app over httpx's ASGI transport,
    which reassembles the whole body before returning it -- so iter_bytes here
    reports one chunk however the server sends it, and asserting otherwise
    tests the client. That is the same trap the body limit hit: an in-process
    transport hides transport behaviour.

    What IS in this process is the generator, covered below, and the
    correctness of the bytes, covered here. The socket behaviour was verified
    against real uvicorn: a 32 MiB output arrived in 512 reads, HTTP 200,
    application/octet-stream.
    """
    from fastapi.testclient import TestClient

    size = 8 * 1024 * 1024
    workflow = _service(tmp_path, monkeypatch, output=b"z" * size)
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)
            response = client.get(f"/runs/{run_id}/outputs/tool-1/out")

        assert response.status_code == 200
        assert len(response.content) == size
        assert response.content == b"z" * size
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()


def test_the_response_body_is_a_generator_that_yields_in_chunks(tmp_path,
                                                                monkeypatch):
    """The part that is actually in this process.

    The endpoint returns a StreamingResponse whose iterator reads a megabyte at
    a time, so the peak is one chunk rather than the file. Driven directly
    because the test client would flatten it.
    """
    from fastapi.testclient import TestClient

    size = 8 * 1024 * 1024
    workflow = _service(tmp_path, monkeypatch, output=b"z" * size)
    try:
        with TestClient(main.app) as client:
            run_id = _finish(client, workflow, main.RUNS)

        async def collect():
            response = await main.stream_output(run_id, "tool-1", "out")
            # Starlette wraps a sync generator into an async one, so it is
            # consumed as such rather than with list().
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.new_event_loop().run_until_complete(collect())
        assert len(chunks) > 1, (
            f"the whole output was yielded as {len(chunks)} chunk(s); the "
            f"generator is not streaming"
        )
        assert max(len(chunk) for chunk in chunks) <= 1024 * 1024
        assert b"".join(chunks) == b"z" * size
    finally:
        convert.tools.clear()
        main.RETAINED.release_all()
