"""How an input reaches a run (#12).

Before this, one way only -- and the code said so in its own names.

Every input arrives as bytes in a multipart request. The handler reads each part
whole, hands perform_run a list of (filename, bytes), and the run writes them
into its workspace. The function that decides what a run needs is called
expected_UPLOADS, and the parameter is called `uploads`: the single supported
source is spelled into the vocabulary, so a second one cannot be added without
either renaming things or lying about what they mean.

That matters beyond tidiness. D1 exists because F1 (htsget) and F2 (DRS) fetch
their inputs from elsewhere -- a slice streamed from a server, an object resolved
by identifier -- and neither of those is a thing a browser pushes. A file already
sitting on the agent's disk, which is the ordinary case inside a TRE, cannot be
used at all today without uploading it to the machine it is already on.

It also puts a ceiling on size that has nothing to do with the tool: every input
is held in memory in its entirety, twice over -- once as the request body, once
as the list handed to the run.
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

import convert
import main


import json
import os

import pytest

from datasource import (DataSource, DataSourceError, LocalPathSource,
                        PROVIDERS, UploadSource, get_sources)
from workspace import make_workspace


# --------------------------------------------------------------------------
# the interface, and what a third provider costs


def test_the_current_sources_are_providers():
    """Four, and two of them exist for D2 rather than for a client to choose.

    `spooled` takes the file starlette already wrote to disk, and `handedover`
    takes one this service wrote while a request was still open -- neither is a
    path a caller supplies.
    """
    assert sorted(PROVIDERS) == ["handedover", "localpath", "spooled", "upload"]
    assert isinstance(get_sources(["upload"])["upload"], UploadSource)


def test_an_unknown_source_stops_the_process():
    """A typo must not silently leave a deployment with sources it did not ask
    for, or without ones it did."""
    with pytest.raises(ValueError) as exc:
        get_sources(["htsget"])
    assert "BIOCHEF_DATA_SOURCES" in str(exc.value)
    assert "upload" in str(exc.value) and "localpath" in str(exc.value)


def test_a_third_provider_needs_no_change_to_the_converter_or_the_runner():
    """D1's acceptance, stated as an actual mock provider.

    It is registered, resolved, and used to satisfy a real run -- and neither
    convert.py nor runner.py is touched to make that work.
    """
    import runner as runner_module

    class MockSource(DataSource):
        name = "mock-for-test"

        def fetch(self, ws, name, spec):
            ws.write_bytes(name, b"from the mock source")

    before_convert = Path(REPO_ROOT / "convert.py").read_text()
    before_runner = Path(REPO_ROOT / "runner.py").read_text()

    PROVIDERS[MockSource.name] = MockSource
    try:
        resolved = get_sources([MockSource.name])
        assert isinstance(resolved[MockSource.name], MockSource)

        import tempfile
        root = tempfile.mkdtemp()
        ws = make_workspace(root)
        try:
            resolved[MockSource.name].fetch(ws, "input-1-out", None)
            with ws.open_read("input-1-out") as handle:
                assert handle.read() == b"from the mock source"
        finally:
            ws.cleanup()
    finally:
        del PROVIDERS[MockSource.name]

    assert Path(REPO_ROOT / "convert.py").read_text() == before_convert
    assert Path(REPO_ROOT / "runner.py").read_text() == before_runner
    assert "DataSource" not in before_convert
    assert "DataSource" not in before_runner


def test_the_converter_still_knows_nothing_about_sources():
    """It answers which NAMES a run needs, with no opinion on their origin.

    Demonstrated rather than grepped. An earlier version of this looked for the
    word "upload" in the source and matched the function's own name -- the
    fourth time in this suite a check has matched a name instead of a
    behaviour.
    """
    workflow = convert.Workflow(nodes=[
        convert.Node(id="tool-1", bin="t",
                     inputs={"input-1-out": convert.IO()},
                     outputs={"tool-1-out": convert.IO()}),
    ])

    assert convert.expected_uploads(workflow) == {"input-1-out"}

    # The same answer regardless of which sources exist: it never consults them.
    import datasource

    original = dict(datasource.PROVIDERS)
    try:
        datasource.PROVIDERS.clear()
        assert convert.expected_uploads(workflow) == {"input-1-out"}
    finally:
        datasource.PROVIDERS.update(original)

    assert "datasource" not in Path(REPO_ROOT / "convert.py").read_text()


# --------------------------------------------------------------------------
# upload, unchanged


def test_the_upload_source_writes_the_bytes_it_is_given(tmp_path):
    ws = make_workspace(str(tmp_path))
    try:
        UploadSource().fetch(ws, "input-1-out", b"pushed from the browser")
        with ws.open_read("input-1-out") as handle:
            assert handle.read() == b"pushed from the browser"
    finally:
        ws.cleanup()


def test_the_upload_source_refuses_something_that_is_not_bytes(tmp_path):
    ws = make_workspace(str(tmp_path))
    try:
        with pytest.raises(DataSourceError, match="expects bytes"):
            UploadSource().fetch(ws, "input-1-out", "/etc/passwd")
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# localpath, which is the one that can go badly wrong


def test_localpath_refuses_to_exist_without_a_root():
    """The client chooses the path. A source that could read anywhere is an
    arbitrary-file-read with a workflow engine attached."""
    with pytest.raises(ValueError, match="BIOCHEF_LOCAL_ROOT"):
        LocalPathSource(root="")


def test_localpath_copies_a_file_from_inside_its_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "reads.fastq").write_bytes(b"@read1\nACGT\n")

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        LocalPathSource(root=str(root)).fetch(ws, "input-1-out", "reads.fastq")
        with ws.open_read("input-1-out") as handle:
            assert handle.read() == b"@read1\nACGT\n"
    finally:
        ws.cleanup()


@pytest.mark.parametrize("escape", [
    "../outside.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "subdir/../../outside.txt",
])
def test_localpath_refuses_a_path_that_leaves_its_root(tmp_path, escape):
    root = tmp_path / "data"
    (root / "subdir").mkdir(parents=True)
    (tmp_path / "outside.txt").write_bytes(b"not yours")

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        with pytest.raises(DataSourceError, match="outside"):
            LocalPathSource(root=str(root)).fetch(ws, "input-1-out", escape)
    finally:
        ws.cleanup()


def test_localpath_refuses_a_symlink_that_points_out_of_its_root(tmp_path):
    """Resolved before it is checked, not after.

    A symlink inside the root pointing outside it passes any test done on the
    path as written, which is why realpath comes first.
    """
    root = tmp_path / "data"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    os.symlink(str(secret), str(root / "innocent.txt"))

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        with pytest.raises(DataSourceError, match="outside"):
            LocalPathSource(root=str(root)).fetch(ws, "input-1-out",
                                                  "innocent.txt")
    finally:
        ws.cleanup()


def test_localpath_refuses_a_directory(tmp_path):
    root = tmp_path / "data"
    (root / "adirectory").mkdir(parents=True)

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        with pytest.raises(DataSourceError, match="not a file"):
            LocalPathSource(root=str(root)).fetch(ws, "input-1-out",
                                                  "adirectory")
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# streaming, which is why a provider writes rather than returns


def test_a_workspace_can_be_written_from_a_stream_without_holding_it(tmp_path):
    """The point of the interface taking a workspace.

    A provider returning bytes would put every input through memory whole, which
    is the ceiling D2 exists to lift. This copies in chunks and never holds more
    than one.
    """
    source_file = tmp_path / "big"
    with open(source_file, "wb") as handle:
        for _ in range(64):
            handle.write(b"x" * 65536)          # 4 MiB

    largest = {"chunk": 0}

    class _Watching:
        def __init__(self, path):
            self._handle = open(path, "rb")

        def read(self, size=-1):
            data = self._handle.read(size)
            largest["chunk"] = max(largest["chunk"], len(data))
            return data

        def close(self):
            self._handle.close()

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        watcher = _Watching(source_file)
        try:
            written = ws.write_stream("input-1-out", watcher)
        finally:
            watcher.close()

        assert written == 4 * 1024 * 1024
        assert largest["chunk"] <= 1024 * 1024, (
            f"read {largest['chunk']} bytes at once; the file was not streamed"
        )
        with ws.open_read("input-1-out") as handle:
            assert len(handle.read()) == 4 * 1024 * 1024
    finally:
        ws.cleanup()


def test_a_streamed_write_keeps_the_same_refusals_as_a_written_one(tmp_path):
    """Same descriptor, same O_EXCL. Streaming must not be a way around it."""
    import io

    ws = make_workspace(str(tmp_path))
    try:
        ws.write_bytes("input-1-out", b"first")
        with pytest.raises(FileExistsError):
            ws.write_stream("input-1-out", io.BytesIO(b"second"))
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# through the handler, with a source that is not upload


def test_a_run_can_take_an_input_from_a_non_upload_source(tmp_path, monkeypatch):
    """The acceptance criterion, exercised end to end.

    Every other test here drives a provider directly, so nothing noticed
    whether the HANDLER consulted the source a client named -- it could have
    used `upload` for everything and passed the lot.
    """
    import hashlib

    import main

    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "reads.fastq").write_bytes(b"already on this host")

    monkeypatch.setitem(main.SOURCES, "localpath",
                        LocalPathSource(root=str(data_root)))
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
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

    seen = {}

    def capture(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        with open(os.path.join(ws.path, "input-1-out"), "rb") as handle:
            seen["input"] = handle.read()
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as handle:
            handle.write(b"ok")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", capture)

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
        results = main.perform_run(
            workflow, [("localpath", "input-1-out", "reads.fastq")])
    finally:
        convert.tools.clear()

    assert seen["input"] == b"already on this host", (
        "the handler did not use the source the client named"
    )
    assert "tool-1" in results


def test_a_source_the_deployment_does_not_permit_is_refused(tmp_path, monkeypatch):
    """Naming localpath on a deployment that allows only uploads is a 400.

    Not a 500, and not a silent fallback to uploading -- a client asking for
    something the operator has not enabled should be told so.
    """
    import main
    from fastapi import HTTPException

    import hashlib

    monkeypatch.setattr(main, "SOURCES", {"upload": UploadSource()})
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    # A workflow with a real tool node, so the name IS one this run expects.
    # Without that the name check fires first and this would pass for the wrong
    # reason -- an empty workflow consumes nothing, so every name is rejected.
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
        with pytest.raises(HTTPException) as exc:
            main.perform_run(workflow,
                             [("localpath", "input-1-out", "reads.fastq")])
    finally:
        convert.tools.clear()

    assert exc.value.status_code == 400
    assert "does not permit" in str(exc.value.detail), exc.value.detail
