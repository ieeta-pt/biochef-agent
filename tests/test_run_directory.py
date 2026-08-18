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
import threading
import time

import pytest
from fastapi.testclient import TestClient

import convert
import main
from workspace import UnsafeName, make_workspace


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


def test_a_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    """Drives main.run_snakemake itself.

    An earlier version of this test re-implemented Popen/getpgid/killpg inline
    and asserted about its own local copy, so it passed against a run_snakemake
    gutted to `return 0, "", ""` and did not notice killpg being replaced by
    process.kill(). A test that never calls the function it names is worse than
    no test, because the argument for group-killing sits right next to it and a
    reader takes the test as evidence.

    The run is on a thread with a bounded join: the failure mode being guarded
    against is a hang, and called directly a regression would hang the suite
    instead of failing it.
    """
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 300 &\necho $! > grandchild.pid\nwait\n")
    script.chmod(0o755)

    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        # Substitute ONLY the snakemake invocation. `main.subprocess` is the
        # subprocess module itself, so patching Popen through it is global --
        # an earlier version of this test replaced every Popen in the process,
        # which meant subprocess.run(["kill", ...]) further down launched the
        # slow helper instead of kill. It then reported a surviving grandchild
        # that was really a second helper it had just started itself, and left
        # `sleep 300` running until the suite timed out five minutes later.
        if argv and "snakemake" in str(argv[0]):
            return real_popen([str(script)], **kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)

    ws = make_workspace(str(tmp_path))
    result = {}

    def run():
        result["value"] = main.run_snakemake(ws, timeout_s=1)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(15)

    try:
        assert not thread.is_alive(), "run_snakemake never returned -- it hung"
        assert result["value"][0] == -signal.SIGKILL

        pid_file = Path(ws.path) / "grandchild.pid"
        assert pid_file.exists(), "the helper never spawned its grandchild"
        grandchild = int(pid_file.read_text().strip())

        # os.kill rather than shelling out, so this cannot be affected by the
        # Popen patch above at all.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the grandchild outlived the kill")
    finally:
        ws.cleanup()


def test_a_symlinked_slot_is_refused_on_read(tmp_path):
    """O_NOFOLLOW, which is what closes #41.

    Without it the suite stayed green: a tool that replaces its own output with
    a symlink had the target's contents base64'd into the response with HTTP 200.
    """
    secret = tmp_path / "SECRET"
    secret.write_bytes(b"private-key-material")

    ws = make_workspace(str(tmp_path))
    try:
        os.symlink(str(secret), os.path.join(ws.path, "tool-out"))
        with pytest.raises(UnsafeName, match="symbolic link"):
            ws.open_read("tool-out")
    finally:
        ws.cleanup()


def test_cleanup_declines_when_the_path_no_longer_names_the_workspace(tmp_path):
    """rmtree takes a path, and a path is a lookup rather than a handle.

    If the directory is moved or replaced after it was opened, the path names
    something else, and deleting it would destroy whatever is there instead. The
    descriptor is held so the run's identity does not depend on the path, so
    cleanup compares the two and declines if they differ.

    Leaking a directory is the right failure here. Deleting the wrong one is not.
    """
    ws = make_workspace(str(tmp_path))
    original = ws.path

    moved = str(tmp_path / "moved-away")
    os.rename(original, moved)
    impostor = Path(original)
    impostor.mkdir()
    (impostor / "someone-elses-data").write_bytes(b"do not delete")

    ws.cleanup()

    assert (impostor / "someone-elses-data").exists(), \
        "cleanup deleted a directory that was not its workspace"
    shutil.rmtree(moved, ignore_errors=True)
    shutil.rmtree(str(impostor), ignore_errors=True)


def test_cleanup_removes_its_own_workspace(tmp_path):
    """The identity check must not stop cleanup doing its job."""
    ws = make_workspace(str(tmp_path))
    ws.write_bytes("something", b"x")
    path = ws.path

    ws.cleanup()

    assert not os.path.exists(path)


def test_a_hardlinked_slot_is_refused(tmp_path):
    """The same exfiltration as #41, by hard link rather than symbolic link.

    O_NOFOLLOW does not stop this: a hard link is not a symlink, it is another
    name for the same inode and indistinguishable from the original. Without the
    link-count check, a tool could link a file from outside the run into its own
    output slot and the agent would read it and base64 it into the response.

    That is the case the threat model has to get right. The tool binary is the
    untrusted party here -- it is arbitrary code pulled from a registry, running
    against whatever the deployment can see -- so "the attacker already runs code
    on the host" describes the ordinary situation rather than an escalation. In
    an environment where the agent sits next to data that may not leave, the
    response body is the way out.
    """
    outside = tmp_path / "not-for-export"
    outside.write_bytes(b"controlled data")

    ws = make_workspace(str(tmp_path))
    try:
        os.link(str(outside), os.path.join(ws.path, "tool-out"))
        with pytest.raises(UnsafeName, match="another name"):
            ws.open_read("tool-out")
    finally:
        ws.cleanup()


def test_a_file_the_workspace_made_itself_is_readable(tmp_path):
    """The link-count check must not refuse ordinary output."""
    ws = make_workspace(str(tmp_path))
    try:
        ws.write_bytes("genuine", b"produced by the run")
        with ws.open_read("genuine") as handle:
            assert handle.read() == b"produced by the run"
    finally:
        ws.cleanup()


def test_a_symlinked_slot_is_refused_on_a_non_exclusive_write(tmp_path):
    """The write path without O_EXCL in front of it.

    /convert never passes exclusive=False, so O_EXCL normally refuses a
    symlinked slot before O_NOFOLLOW is reached -- which means the exclusive
    path cannot show whether O_NOFOLLOW is present at all. This one can.
    """
    victim = tmp_path / "VICTIM"
    victim.write_bytes(b"do-not-clobber")

    ws = make_workspace(str(tmp_path))
    try:
        os.symlink(str(victim), os.path.join(ws.path, "slot"))
        with pytest.raises((UnsafeName, OSError)):
            ws.write_bytes("slot", b"clobbered", exclusive=False)
        assert victim.read_bytes() == b"do-not-clobber"
    finally:
        ws.cleanup()


def test_every_open_goes_through_the_held_descriptor(tmp_path):
    """The descriptor is opened once and pinned.

    Re-deriving it from the path on each call survives the rest of the suite,
    even though the class docstring says it is held precisely to stop the
    directory being swapped mid-run. Structural rather than behavioural because
    the race needs local filesystem access, so this is cheap insurance.
    """
    import inspect

    source = inspect.getsource(main.make_workspace.__module__ and type(
        make_workspace(str(tmp_path))))
    assert "dir_fd=self._fd" in source, "_open must use the held descriptor"
    assert "realpath" not in source.split("def _open")[1].split("def ")[0], \
        "_open must not re-resolve the path"


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
