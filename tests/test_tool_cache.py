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

import hashlib
import json
import time
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


BINARY = "#!/bin/sh\n"


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeRegistry:
    """Stands in for the ORAS client, recording what was pulled and where.

    It also answers get_manifest, because the pull is now checked against the
    manifest before the staged directory is promoted (#9). The digests here are
    computed from the same bytes pull writes, so an honest registry verifies --
    a test that hardcoded them would drift and start failing for the wrong
    reason.
    """

    def __init__(self):
        self.pulls = []
        self.bundle_bytes = json.dumps(BUNDLE).encode()
        self.binary_bytes = BINARY.encode()

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
        self.pulls.append((target, outdir))
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "bundle.json"), "wb") as handle:
            handle.write(self.bundle_bytes)
        with open(os.path.join(outdir, "tool"), "wb") as handle:
            handle.write(self.binary_bytes)


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
    cache = Path(convert.TOOL_CACHE) / "tool"

    assert ".part." in outdir, "the pull must be staged, not written in place"
    assert outdir != str(cache) + ".part", (
        "the staging name must be unique per attempt -- a fixed one is shared "
        "between concurrent runs, which is what let them delete each other's work"
    )
    assert (cache / "bundle.json").exists(), "the staged directory was not moved into place"
    assert not list(cache.parent.glob("tool.part*")), "a staging directory was left behind"


def test_a_second_node_does_not_pull_again(registry):
    convert.fetch_tool("tool-1", "some/repo")
    convert.fetch_tool("tool-2", "some/repo")

    assert len(registry.pulls) == 1, "the memo did not prevent a second pull"


def test_a_leftover_staging_directory_from_an_interrupted_pull_is_discarded(
        registry, tmp_path):
    """A previous run killed mid-pull leaves a staging directory behind.

    It must never be reused, because mixing two pulls together would produce a
    bundle that matches no manifest -- or worse, one that does.

    The mechanism changed: this used to be an rmtree before makedirs of a fixed
    ".part" name, and that fixed name was exactly what let concurrent runs
    delete each other's work. Staging names are now unique per attempt, so a
    leftover is not reused because nothing ever looks at it again. The property
    is the same; the reason is not.

    A hard kill can still leave one on disk. Nothing reads it, and the next
    attempt neither reuses nor trips over it.
    """
    staging = Path(convert.TOOL_CACHE) / "tool.part"
    staging.mkdir(parents=True)
    (staging / "stale-file").write_text("from an interrupted pull")

    convert.fetch_tool("tool-1", "some/repo")

    cache = Path(convert.TOOL_CACHE) / "tool"
    assert (cache / "bundle.json").exists()
    assert not (cache / "stale-file").exists(), "content from the interrupted pull survived"


def test_a_failed_pull_takes_its_staging_directory_with_it(registry, tmp_path):
    """A refused bundle must not be left where anything could find it.

    The promote is what makes a pull visible, so an unverified one is only ever
    in staging -- but staging is beside the cache, and leaving it there would
    mean bytes nobody vouched for sitting next to bytes that were vouched for,
    waiting for someone to write the wrong glob.
    """
    def failing_pull(target, outdir):
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "half-written"), "w") as handle:
            handle.write("interrupted")
        raise RuntimeError("the registry went away")

    registry.pull = failing_pull

    with pytest.raises(RuntimeError):
        convert.fetch_tool("tool-1", "some/repo")

    cache_root = Path(convert.TOOL_CACHE)
    leftovers = list(cache_root.glob("tool.part*")) if cache_root.exists() else []
    assert not leftovers, f"staging survived a failed pull: {leftovers}"


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


def test_many_threads_racing_a_cold_cache_all_succeed(registry, tmp_path):
    """One pull, and nobody trips over anybody.

    Twenty concurrent runs used to leave one COMPLETE and nineteen
    SYSTEM_ERROR, because they all staged into the same fixed ".part" and
    deleted each other's work. This is that, at the level it actually happens.
    """
    import threading

    outcome = {"ok": 0, "failed": []}
    barrier = threading.Barrier(20)
    guard = threading.Lock()

    def race():
        barrier.wait()
        try:
            convert.fetch_tool("tool-1", "some/repo")
            with guard:
                outcome["ok"] += 1
        except Exception as failure:                 # noqa: BLE001
            with guard:
                outcome["failed"].append(repr(failure))

    threads = [threading.Thread(target=race) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcome["failed"] == [], outcome["failed"]
    assert outcome["ok"] == 20
    assert len(registry.pulls) == 1, (
        f"{len(registry.pulls)} pulls for one tool; the lock should mean one"
    )


def test_the_bundle_is_read_while_the_lock_is_held(registry):
    """The promote deletes the cached directory before replacing it.

    A reader arriving in that window finds nothing there, so the read has to be
    inside the lock rather than after it. Cross-process readers are a separate
    matter, recorded in the module.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(convert._fetch_tool_once)))

    def reads_bundle(node):
        return any(isinstance(n, ast.Constant) and n.value == "bundle.json"
                   for n in ast.walk(node))

    inside = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and "_fetch_lock" in ast.dump(node.items[0].context_expr)
        and any(reads_bundle(child) for child in node.body)
    ]
    assert inside, (
        "the bundle read is not inside the lock that guards the promote. "
        "Checked by scope, not by line order: an earlier version of this test "
        "compared string offsets, so dedenting the read out of the with-block "
        "left it passing while the race it guards was wide open."
    )


RACER = """
import hashlib, json, os, sys, time, types
sys.path.insert(0, sys.argv[3])
oras = types.ModuleType("oras"); cm = types.ModuleType("oras.client")
class _C:
    def __init__(s, *a, **k): pass
    def login(s, *a, **k): pass
cm.OrasClient = _C; oras.client = cm
sys.modules["oras"] = oras; sys.modules["oras.client"] = cm
import convert
B = {"id": "tool", "name": "tool", "bin": "tool",
     "io": {"inputs": [], "outputs": []}, "parameters": []}
bb = json.dumps(B).encode(); nb = b"#!/bin/sh\\n"
dig = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()
class Reg:
    def get_container(s, t): return t
    def get_manifest(s, c, *a, **k):
        return {"layers": [
            {"digest": dig(bb), "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "bundle.json"}},
            {"digest": dig(nb), "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "tool"}}]}
    def pull(s, target, outdir):
        os.makedirs(outdir, exist_ok=True); time.sleep(0.15)
        open(os.path.join(outdir, "bundle.json"), "wb").write(bb)
        open(os.path.join(outdir, "tool"), "wb").write(nb)
convert.TOOL_CACHE = sys.argv[1]; convert.client = Reg(); convert.tools.clear()
gate = float(sys.argv[2])
while time.time() < gate:
    time.sleep(0.002)
bundle = convert.fetch_tool("tool-1", "r")
assert bundle["bin"] == "tool"
"""


def test_separate_processes_sharing_a_cache_do_not_break_each_other(tmp_path):
    """The promote is two syscalls, and a threading.Lock does not cross processes.

    Six processes on one cold cache used to leave two failing in every trial
    with ENOTEMPTY: os.replace will not overwrite a non-empty directory, and
    each had just rmtree'd it for the other. Whoever loses that footrace holds
    a verified bundle and finds someone else's equally verified bundle in
    place, so losing is not a failure -- it is a reason to use theirs.

    Subprocesses rather than threads, deliberately: threads share the lock,
    and the lock is exactly what does not reach across processes.
    """
    import subprocess
    import sys as _sys

    cache = tmp_path / "shared-cache"
    cache.mkdir()
    racer = tmp_path / "racer.py"
    racer.write_text(RACER)

    gate = time.time() + 1.5
    processes = [
        subprocess.Popen(
            [_sys.executable, str(racer), str(cache), str(gate), str(REPO_ROOT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    outcomes = [p.communicate() for p in processes]

    failures = [err[-300:] for p, (out, err) in zip(processes, outcomes)
                if p.returncode != 0]
    assert not failures, failures
