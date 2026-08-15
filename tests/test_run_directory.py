"""Every run gets its own directory, and the process never moves (#40).

The commit before this one recorded what happened previously: a failed request
left the process inside `tmp/`, the next request created `tmp/tmp/`, and every
run wrote to the same fixed path with nothing naming the run. Each test here is
the closed form of one of those.

The change that makes it possible is `snakemake -s <file> -d <dir>`: the
Snakefile and the working directory are passed explicitly, so relative paths in
the rules resolve under `-d` and the shell blocks run with that as their cwd.
Nothing has to move the process.
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

import base64
import json
import os
import shutil
import signal
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import convert
import main
from workspace import make_workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, workflow=b"{}", filename="input-1-out"):
    return client.post(
        "/convert",
        data={"biochef_workflow": workflow},
        files=[("files", (filename, b"payload", "text/plain"))],
    )


def test_a_failed_request_no_longer_moves_the_process(client, tmp_path):
    before = os.getcwd()
    post(client, workflow=b"this is not json at all")
    assert os.getcwd() == before


def test_nothing_nests(client, tmp_path):
    post(client, workflow=b"not json")
    post(client, workflow=b"not json")

    assert not (tmp_path / "tmp" / "tmp").exists()
    assert not (tmp_path / "tmp").exists(), "no shared run directory is created at all"


def test_a_failed_run_leaves_no_workspace_behind(client, tmp_path, monkeypatch):
    """The finally removes the directory whether the run worked or not.

    Previously the last line of the happy path restored global state, so a
    failure skipped it. There is no global state now, only a directory.
    """
    made = []
    real = main.make_workspace
    monkeypatch.setattr(main, "make_workspace",
                        lambda root=None: made.append(real(root)) or made[-1])

    post(client, workflow=b"not json")

    assert made, "the handler should have made a workspace"
    assert not os.path.exists(made[0].path), "and removed it on the way out"


def test_two_runs_get_different_directories(client, tmp_path, monkeypatch):
    made = []
    real = main.make_workspace
    monkeypatch.setattr(main, "make_workspace",
                        lambda root=None: made.append(real(root)) or made[-1])

    post(client, workflow=b"not json")
    post(client, workflow=b"not json")

    assert len(made) == 2
    assert made[0].path != made[1].path, "each run needs a directory of its own"


def test_the_workspace_is_private(tmp_path):
    ws = make_workspace(str(tmp_path))
    try:
        assert oct(os.stat(ws.path).st_mode & 0o777) == "0o700"
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# the timeout, which is only correct if it kills the group


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    """Killing only snakemake leaves the tool running and then blocks forever.

    That is what subprocess.run(timeout=N) does internally, so this is worth an
    explicit test: without start_new_session and killpg, adding a timeout makes
    the service worse rather than better.
    """
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 300 &\nwait\n")
    script.chmod(0o755)

    started = time.time()
    process = subprocess.Popen([str(script)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    pgid = os.getpgid(process.pid)
    try:
        process.communicate(timeout=1)
        pytest.fail("the helper should not have finished")
    except subprocess.TimeoutExpired:
        os.killpg(pgid, signal.SIGKILL)
        process.communicate(timeout=5)

    assert time.time() - started < 5, "communicate did not return promptly"

    leftover = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True)
    assert leftover.returncode != 0, "something in the group survived"


SNAKEMAKE = shutil.which("snakemake")

BUNDLE = {
    "id": "echoer", "name": "echoer", "bin": "echoer",
    "io": {"inputs": [{"name": "in", "types": ["TEXT"], "mode": "file"}],
           "outputs": [{"name": "out", "types": ["TEXT"], "mode": "stdout"}]},
    "parameters": [],
}


@pytest.mark.skipif(not SNAKEMAKE, reason="snakemake is not installed")
def test_a_whole_run_works_end_to_end(tmp_path, monkeypatch):
    """The handler, a real snakemake, and a real tool.

    Everything else here stubs the run, so without this nothing would show that
    the rewritten handler actually executes a workflow -- only that its parts
    have the right shapes.
    """
    cache = tmp_path / "cache" / "echoer"
    cache.mkdir(parents=True)
    (cache / "bundle.json").write_text(json.dumps(BUNDLE))
    tool = cache / "echoer"
    tool.write_text("#!/usr/bin/env python3\n"
                    "import sys\n"
                    "sys.stdout.write(open(sys.argv[1]).read().upper())\n")
    tool.chmod(0o755)

    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    monkeypatch.setattr(main, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()

    workflow = {
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": "echoer-1", "type": "workflowNode",
             "data": {"label": "echoer", "repo": "r", "paramValues": {}, "outputs": {}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": "echoer-1", "targetHandle": "in"},
            {"source": "echoer-1", "sourceHandle": "out",
             "target": "output-1", "targetHandle": "in"},
        ],
    }

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/convert",
        data={"biochef_workflow": json.dumps(workflow)},
        files=[("files", ("input-1-out", b"hello", "text/plain"))],
    )

    assert response.status_code == 200, response.text
    encoded = response.json()["echoer-1"]["out"]
    assert base64.b64decode(encoded) == b"HELLO"

    # and the run left nothing behind
    runs = tmp_path / "runs"
    assert not any(runs.iterdir()) if runs.exists() else True


def test_two_nodes_sharing_a_binary_are_placed_once(tmp_path, monkeypatch):
    """The most ordinary pipeline in the catalogue.

    80 of the 176 operations share an executable with another -- all 15 samtools
    operations, all 20 seqtk ones -- so "samtools sort" into "samtools index" is
    two nodes naming one binary. Placing per node made the second fail O_EXCL and
    the request 500. No test in the original change used more than one tool node,
    which is why it got through.
    """
    cache = tmp_path / "cache" / "samtools"
    cache.mkdir(parents=True)
    (cache / "samtools").write_text("#!/bin/sh\n")
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    class Node:
        def __init__(self, node_id, binary):
            self.id, self.bin = node_id, binary

    class Flow:
        def __init__(self, nodes):
            self.nodes = nodes

    ws = make_workspace(str(tmp_path))
    try:
        convert.materialise_tools(
            Flow([Node("samtools-1", "samtools"), Node("samtools-2", "samtools")]), ws)
        assert (Path(ws.path) / "samtools").exists()
    finally:
        ws.cleanup()


def test_run_snakemake_is_given_the_directory_explicitly(tmp_path, monkeypatch):
    """The argv is the whole point: -s and -d instead of moving the process."""
    ws = make_workspace(str(tmp_path))
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
            captured["new_session"] = kwargs.get("start_new_session")
            self.pid = os.getpid()
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    # main.os IS the os module, so patching through it patches os.getpgid
    # globally -- a lambda calling os.getpgid would then call itself. Keep the
    # real one.
    real_getpgid = os.getpgid
    monkeypatch.setattr(main.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(main.os, "getpgid", lambda pid: real_getpgid(os.getpid()))
    try:
        main.run_snakemake(ws)
    finally:
        ws.cleanup()

    assert "-s" in captured["argv"] and "-d" in captured["argv"]
    assert captured["argv"][captured["argv"].index("-d") + 1] == ws.path
    assert captured["cwd"] == ws.path
    assert captured["new_session"] is True
