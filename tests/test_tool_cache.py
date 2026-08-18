"""The shared tool cache, and what happens when a run fails (#40).

Both were entirely uncovered. `fetch_tool`'s staging logic is code this branch
adds -- every other test monkeypatches `fetch_tool` away or pre-populates the
cache by hand, so not one line of the real function ran. And the branch that
turns a non-zero snakemake exit into a 500 was never taken: replacing
`if code != 0:` with `if False:` left the suite green at 36 passed.

A tool exiting non-zero is the single most common real outcome of a run, so it
is worth pinning what the client is told about it.
"""

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

import json
import os

import pytest
from fastapi.testclient import TestClient

import convert
import main

BUNDLE = {"id": "tool", "name": "tool", "bin": "tool",
          "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
                 "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
          "parameters": []}

# The handler now refuses an upload the workflow does not declare, so a test
# that posts a file needs a workflow that asks for one.
WORKFLOW = json.dumps({
    "nodes": [
        {"id": "input-1", "type": "inputWorkflowNode",
         "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
        {"id": "tool-1", "type": "workflowNode",
         "data": {"label": "tool", "repo": "r", "paramValues": {}, "outputs": {}}},
        {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
    ],
    "edges": [
        {"source": "input-1", "sourceHandle": "out", "target": "tool-1", "targetHandle": "in"},
        {"source": "tool-1", "sourceHandle": "out", "target": "output-1", "targetHandle": "in"},
    ],
})


class FakeRegistry:
    """Stands in for the ORAS client, recording what was pulled and where."""

    def __init__(self):
        self.pulls = []

    def pull(self, target, outdir):
        self.pulls.append((target, outdir))
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "bundle.json"), "w") as handle:
            json.dump(BUNDLE, handle)
        with open(os.path.join(outdir, "tool"), "w") as handle:
            handle.write("#!/bin/sh\n")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(convert, "client", fake)
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    convert.tools.clear()
    return fake


def test_a_bundle_is_pulled_into_the_cache_and_staged_first(registry, tmp_path):
    """Staged as .part and moved with os.replace.

    So an interrupted pull cannot leave a half-written bundle that the next run
    reads as complete -- and the move is the point a digest check belongs (#9).
    """
    bundle = convert.fetch_tool("tool-1", "some/repo")

    assert bundle["bin"] == "tool"
    assert len(registry.pulls) == 1
    target, outdir = registry.pulls[0]
    assert outdir.endswith(".part"), "the pull must be staged, not written in place"

    cache = Path(convert.TOOL_CACHE) / "tool"
    assert (cache / "bundle.json").exists(), "the staged directory was not moved into place"
    assert not Path(str(cache) + ".part").exists(), "the staging directory was left behind"


def test_a_second_node_does_not_pull_again(registry):
    convert.fetch_tool("tool-1", "some/repo")
    convert.fetch_tool("tool-2", "some/repo")

    assert len(registry.pulls) == 1, "the memo did not prevent a second pull"


def test_a_leftover_staging_directory_from_an_interrupted_pull_is_discarded(
        registry, tmp_path):
    """The reason for rmtree before makedirs.

    A previous run killed mid-pull leaves a .part behind; reusing it would mix
    two pulls together.
    """
    staging = Path(convert.TOOL_CACHE) / "tool.part"
    staging.mkdir(parents=True)
    (staging / "stale-file").write_text("from an interrupted pull")

    convert.fetch_tool("tool-1", "some/repo")

    cache = Path(convert.TOOL_CACHE) / "tool"
    assert (cache / "bundle.json").exists()
    assert not (cache / "stale-file").exists(), "content from the interrupted pull survived"


def test_a_cached_bundle_on_disk_is_not_pulled_again(registry, tmp_path):
    """The memo is per-process; the cache has to survive a restart too."""
    convert.fetch_tool("tool-1", "some/repo")
    convert.tools.clear()               # as if the process restarted

    convert.fetch_tool("tool-1", "some/repo")

    assert len(registry.pulls) == 1, "a bundle already on disk was pulled again"


def test_a_tool_id_that_is_not_a_plain_name_is_refused(registry):
    from workspace import UnsafeName

    with pytest.raises(UnsafeName):
        convert.fetch_tool("../escape-1", "some/repo")


# --------------------------------------------------------------------------
# what the client is told when the run fails


def test_a_failed_run_is_reported_as_execution_failed(tmp_path, monkeypatch):
    """The branch that `if False:` proved nothing exercised.

    A tool exiting non-zero is the ordinary failure, so both the status and the
    shape of the body are worth pinning -- including that stderr is echoed back,
    which is a deliberate choice rather than an accident.
    """
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    os.makedirs(tmp_path / "cache" / "tool", exist_ok=True)
    (tmp_path / "cache" / "tool" / "tool").write_text("#!/bin/sh\n")

    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "run_snakemake",
                        lambda ws, *a, **k: (1, "", "the tool said no"))
    monkeypatch.chdir(tmp_path)

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/convert",
        data={"biochef_workflow": WORKFLOW},
        files=[("files", ("input-1-out", b"x", "text/plain"))],
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "execution_failed"
    assert detail["exit_code"] == 1
    assert "the tool said no" in detail["stderr_tail"]


def test_a_failed_run_still_removes_its_workspace(tmp_path, monkeypatch):
    """The finally has to run on this path too, not only on a parse error."""
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    os.makedirs(tmp_path / "cache" / "tool", exist_ok=True)
    (tmp_path / "cache" / "tool" / "tool").write_text("#!/bin/sh\n")

    made = []
    real = main.make_workspace
    monkeypatch.setattr(main, "make_workspace",
                        lambda root=None: made.append(real(root)) or made[-1])
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "run_snakemake", lambda ws, *a, **k: (2, "", "boom"))
    monkeypatch.chdir(tmp_path)

    client = TestClient(main.app, raise_server_exceptions=False)
    client.post(
        "/convert",
        data={"biochef_workflow": WORKFLOW},
        files=[("files", ("input-1-out", b"x", "text/plain"))],
    )

    assert made, "no workspace was created"
    assert not os.path.exists(made[0].path), "a failed run left its workspace behind"
