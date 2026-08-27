"""What a finished run says about how it was produced (#18).

Before this, nothing that outlived it.

E5 asks for a manifest recording the workflow, tool digests, input identities,
timestamps and exit codes, sufficient to re-execute the run and reach the same
outputs. Today a run leaves behind base64 outputs and, since B2, its logs. There
is no record of which tool binaries ran, which bundle versions they came from,
what went in, or when -- and the workspace that held the evidence is deleted.

The pieces exist and are discarded. The agent already:

  * pulls build-evidence.json and sbom.cdx.json alongside bundle.json, because
    pull() writes every layer the manifest lists
  * verifies all three against the registry's manifest digests (#9), so their
    identity is established and then thrown away
  * knows each tool's binary, each declared input, the exit code, and the
    per-step status

None of it is written down. This test file records that, and the shape the
manifest should take: the hub publishes `biochef.build-evidence.v1` with a
`schema` field, digests throughout, and in-toto statements over the artifacts.
A run manifest inventing its own vocabulary would be a second, incompatible
provenance format inside one project.
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


import hashlib
import json
import os

import pytest

from provenance import SCHEMA, _canonical_digest, build
from runs import RunState, RunStore
from workspace import make_workspace


def _digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


BUNDLE = {"id": "tool", "name": "tool", "bin": "tool",
          "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
                 "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
          "parameters": []}

# The shape the hub actually publishes, from bundle_evidence.py.
BUILD_EVIDENCE = {
    "schema": "biochef.build-evidence.v1",
    "generated_at": "2026-08-20T10:00:00+00:00",
    "hub": {"commit": "abc123", "dirty": False},
    "environment": {"python": "3.13.1", "platform": "Linux"},
    "recipe": {"path": "recipes/tool.yaml", "digest": "sha256:" + "1" * 64,
               "id": "tool", "name": "tool", "version": "1.2.3"},
    "operation": {"id": "tool", "bin": "tool", "digest": "sha256:" + "2" * 64},
    "license": {"verified": True, "files": []},
    "runtimes": {},
}

WORKFLOW = {
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
         "targetHandle": "in"}],
}


class _Registry:
    """Publishes what the real one now publishes: three artifacts."""

    def __init__(self, with_evidence=True):
        self.bundle_bytes = json.dumps(BUNDLE).encode()
        self.binary_bytes = b"#!/bin/sh\n"
        self.evidence_bytes = json.dumps(BUILD_EVIDENCE).encode()
        self.with_evidence = with_evidence

    def get_container(self, target):
        return target

    def get_manifest(self, container, *a, **k):
        layers = [
            {"digest": _digest(self.bundle_bytes),
             "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "bundle.json"}},
            {"digest": _digest(self.binary_bytes),
             "mediaType": "application/octet-stream",
             "annotations": {"org.opencontainers.image.title": "tool"}},
        ]
        if self.with_evidence:
            layers.append(
                {"digest": _digest(self.evidence_bytes),
                 "mediaType": "application/octet-stream",
                 "annotations": {"org.opencontainers.image.title":
                                 "build-evidence.json"}})
        return {"layers": layers}

    def pull(self, target, outdir):
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "bundle.json"), "wb") as f:
            f.write(self.bundle_bytes)
        with open(os.path.join(outdir, "tool"), "wb") as f:
            f.write(self.binary_bytes)
        if self.with_evidence:
            with open(os.path.join(outdir, "build-evidence.json"), "wb") as f:
                f.write(self.evidence_bytes)


def _run(tmp_path, monkeypatch, *, with_evidence=True, exit_code=0,
         output=b"the output", expect_failure=False):
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry(with_evidence))
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    convert.tools.clear()
    convert.provenance.clear()

    def produce(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as handle:
            handle.write(output)
        return exit_code, "", ""

    monkeypatch.setattr(main, "run_snakemake", produce)

    captured = {}
    kept = {}

    def keep(ws):
        kept["ws"] = ws
        return True

    from fastapi import HTTPException

    try:
        main.perform_run(json.dumps(WORKFLOW),
                         [("upload", "input-1-out", b"the input")],
                         on_manifest=lambda doc: captured.setdefault("m", doc),
                         retain=keep, run_id="run-under-test")
    except HTTPException:
        if not expect_failure:
            raise
    else:
        assert not expect_failure, "the run was expected to fail and did not"
    return captured.get("m"), kept.get("ws")


# --------------------------------------------------------------------------
# the manifest exists and says what a re-execution would need


def test_a_finished_run_has_a_manifest(tmp_path, monkeypatch):
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    assert manifest["schema"] == SCHEMA
    assert manifest["run_id"] == "run-under-test"
    assert manifest["generated_at"]
    assert manifest["started_at"] and manifest["finished_at"]
    ws.cleanup()


def test_the_manifest_is_written_beside_the_outputs(tmp_path, monkeypatch):
    """So retention keeps them together and releasing a run releases both."""
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
        with ws.open_read("run.json") as handle:
            on_disk = json.loads(handle.read())
    finally:
        convert.tools.clear()

    assert on_disk == manifest
    ws.cleanup()


def test_the_workflow_is_recorded_by_digest_not_by_copy(tmp_path, monkeypatch):
    """A manifest needs to say WHICH workflow, not carry it again."""
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    assert manifest["workflow"]["digest"] == _canonical_digest(WORKFLOW)
    assert "nodes" in manifest["workflow"]
    assert manifest["workflow"]["nodes"][0]["id"] == "tool-1"
    ws.cleanup()


def test_the_workflow_digest_does_not_depend_on_key_order():
    """Two equal documents must hash the same however they were serialised."""
    one = {"nodes": [{"id": "a", "type": "t"}], "edges": []}
    other = {"edges": [], "nodes": [{"type": "t", "id": "a"}]}
    assert _canonical_digest(one) == _canonical_digest(other)


def test_inputs_and_outputs_are_recorded_by_content(tmp_path, monkeypatch):
    try:
        manifest, ws = _run(tmp_path, monkeypatch, output=b"produced")
    finally:
        convert.tools.clear()

    assert manifest["inputs"]["input-1-out"] == _digest(b"the input")
    assert manifest["outputs"]["tool-1"]["tool-1-out"] == _digest(b"produced")
    ws.cleanup()


def test_the_exit_code_is_recorded_on_a_run_that_succeeded(tmp_path, monkeypatch):
    """Not only when it failed, which was the old behaviour."""
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    assert manifest["execution"]["exit_code"] == 0
    assert manifest["execution"]["runner"] == "subprocess"
    ws.cleanup()


# --------------------------------------------------------------------------
# the hub's evidence, carried forward rather than restated


def test_each_tool_carries_the_digests_the_registry_stated(tmp_path, monkeypatch):
    """From the MANIFEST, not by hashing the files again.

    They are the same numbers -- verify_against_manifest has just checked that --
    but taking them from the manifest records what the registry asserted, which
    is what a reader would compare against.
    """
    registry = _Registry()
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    tool = manifest["tools"]["tool-1"]
    assert tool["artifacts"]["bundle.json"] == _digest(registry.bundle_bytes)
    assert tool["artifacts"]["tool"] == _digest(registry.binary_bytes)
    assert tool["artifacts"]["build-evidence.json"] == _digest(
        registry.evidence_bytes)
    ws.cleanup()


def test_the_hubs_build_evidence_is_carried_forward(tmp_path, monkeypatch):
    """Its own vocabulary, not a translation of it.

    The hub publishes biochef.build-evidence.v1; a run manifest inventing
    different words for the same facts would be a second provenance format in
    one project.
    """
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    evidence = manifest["tools"]["tool-1"]["build_evidence"]
    assert evidence["schema"] == "biochef.build-evidence.v1"
    assert evidence["hub"]["commit"] == "abc123"
    assert evidence["recipe"]["version"] == "1.2.3"
    assert evidence["operation"]["digest"] == BUILD_EVIDENCE["operation"]["digest"]
    ws.cleanup()


def test_a_bundle_without_evidence_still_produces_a_manifest(tmp_path,
                                                             monkeypatch):
    """Not every bundle in the registry has been rebuilt with evidence yet.

    Refusing to record a run because its tool predates the hub's SBOM work
    would make provenance a reason not to run things.
    """
    try:
        manifest, ws = _run(tmp_path, monkeypatch, with_evidence=False)
    finally:
        convert.tools.clear()

    tool = manifest["tools"]["tool-1"]
    assert "build_evidence" not in tool
    assert tool["artifacts"]["bundle.json"]
    ws.cleanup()


# --------------------------------------------------------------------------
# E5's acceptance: enough to re-execute and reach the same outputs


def test_the_manifest_names_everything_a_re_execution_would_need(tmp_path,
                                                                 monkeypatch):
    """Checked as a property of the document, not as a promise.

    Re-executing needs four things fixed: which workflow, which tools, which
    inputs, and how it was run. Each is present and each is a digest or a name
    that resolves -- so a reader can obtain the same pieces rather than being
    told they existed.

    It does NOT prove reproducibility. A tool that reads the clock or the
    network will not reproduce whatever this records, and the manifest says
    what was fixed rather than claiming that was everything.
    """
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
    finally:
        convert.tools.clear()

    assert manifest["workflow"]["digest"].startswith("sha256:")

    for node in manifest["workflow"]["nodes"]:
        tool = manifest["tools"][node["id"]]
        assert tool["target"], "a tool with no registry reference"
        assert tool["artifacts"]["bundle.json"].startswith("sha256:")
        # The binary itself, by digest -- the thing that actually ran.
        assert tool["artifacts"][node["bin"]].startswith("sha256:")

    assert manifest["inputs"], "no inputs recorded"
    for name, digest in manifest["inputs"].items():
        assert digest and digest.startswith("sha256:"), name

    assert manifest["execution"]["runner"]
    assert manifest["execution"]["exit_code"] is not None
    ws.cleanup()


def test_identities_are_read_through_the_workspace_not_by_path(tmp_path):
    """So a symlinked file cannot have its target's digest recorded (#41).

    Driven against _identities directly. The end-to-end version tolerated the
    run failing -- which it does, correctly -- and so proved nothing about the
    digest: a manifest that was never built cannot record the wrong thing.
    """
    from provenance import _identities

    secret = tmp_path / "SECRET"
    secret.write_bytes(b"private-key-material")

    ws = make_workspace(str(tmp_path / "runs"))
    try:
        os.symlink(str(secret), os.path.join(ws.path, "tool-1-out"))
        identities = _identities(ws, ["tool-1-out"])
    finally:
        ws.cleanup()

    assert identities["tool-1-out"] != _digest(b"private-key-material"), (
        "the manifest recorded a symlink target as the run's own output"
    )
    assert identities["tool-1-out"] is None, (
        "an unreadable output should be recorded as absent, not guessed at"
    )


def test_a_file_that_is_not_there_is_recorded_as_absent(tmp_path):
    """Not omitted.

    Omitting it leaves a reader to guess whether the file was missing or merely
    unmentioned, and those mean different things about the run.
    """
    from provenance import _identities

    ws = make_workspace(str(tmp_path))
    try:
        ws.write_bytes("present", b"here")
        identities = _identities(ws, ["present", "never-existed"])
    finally:
        ws.cleanup()

    assert identities["present"] == _digest(b"here")
    assert "never-existed" in identities, "a missing file was left out entirely"
    assert identities["never-existed"] is None


def test_the_schema_is_versioned_like_the_hubs():
    """A consumer finding a schema it does not know should say so rather than
    read fields that may have moved."""
    assert SCHEMA == "biochef.run-manifest.v1"
    assert SCHEMA.endswith(".v1")
    assert SCHEMA.startswith("biochef."), (
        "the hub publishes biochef.build-evidence.v1; this should read as part "
        "of the same family"
    )


# --------------------------------------------------------------------------
# the run whose exit code matters most


def test_a_failed_run_gets_a_manifest_too(tmp_path, monkeypatch):
    """It was the only kind that did not.

    E5 asks for a manifest recording exit codes, and the exit code of a run
    that succeeded is always zero. The interesting one was being raised past.
    """
    try:
        manifest, ws = _run(tmp_path, monkeypatch, exit_code=3,
                            expect_failure=True)
    finally:
        convert.tools.clear()

    assert manifest is not None, "a failed run produced no manifest"
    assert manifest["execution"]["exit_code"] == 3
    assert manifest["run_id"] == "run-under-test"
    ws.cleanup()


def test_a_failed_runs_outputs_are_recorded_as_absent(tmp_path, monkeypatch):
    """Not omitted, and not invented.

    A tool that exited non-zero may have produced nothing, or something partial.
    Recording the handles as absent says which outputs were expected and did not
    arrive; leaving them out would say nothing at all.
    """
    try:
        manifest, ws = _run(tmp_path, monkeypatch, exit_code=1,
                            expect_failure=True)
    finally:
        convert.tools.clear()

    assert "tool-1" in manifest["outputs"], (
        "the failing node was left out of the manifest entirely"
    )
    ws.cleanup()


def test_the_inputs_are_still_recorded_when_the_run_fails(tmp_path, monkeypatch):
    """What went in is exactly what someone diagnosing a failure needs."""
    try:
        manifest, ws = _run(tmp_path, monkeypatch, exit_code=2,
                            expect_failure=True)
    finally:
        convert.tools.clear()

    assert manifest["inputs"]["input-1-out"] == _digest(b"the input")
    assert manifest["tools"]["tool-1"]["artifacts"]["bundle.json"]
    ws.cleanup()


# --------------------------------------------------------------------------
# and not written where nobody could read it


def test_no_manifest_is_written_when_nothing_could_fetch_it(tmp_path,
                                                            monkeypatch):
    """The synchronous path deletes its workspace and has no run to attach one
    to, so building one there is a second full read of every input and output
    for a result that provably cannot be reached -- measured at 0.30s of a
    0.57s run for a 64 MiB output.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry())
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "KEEP_WORKSPACE", False)
    convert.tools.clear()
    convert.provenance.clear()

    def produce(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as handle:
            handle.write(b"out")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", produce)

    written = []
    original = main.provenance.write
    monkeypatch.setattr(main.provenance, "write",
                        lambda ws, doc: written.append(doc) or original(ws, doc))

    try:
        results = main.perform_run(
            json.dumps(WORKFLOW), [("upload", "input-1-out", b"in")])
    finally:
        convert.tools.clear()

    assert results["tool-1"], "the run itself should be unaffected"
    assert written == [], (
        "a manifest was written for a run with no id and a workspace about to "
        "be deleted"
    )


def test_a_kept_workspace_does_get_one_even_without_a_run_id(tmp_path,
                                                             monkeypatch):
    """BIOCHEF_KEEP_WORKSPACE is set to look at what a run left behind, and
    what it left behind should include how it was produced."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry())
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(main, "KEEP_WORKSPACE", True)
    convert.tools.clear()
    convert.provenance.clear()

    def produce(ws, timeout_s=None, on_start=None, on_finish=None, on_line=None):
        with open(os.path.join(ws.path, "tool-1-out"), "wb") as handle:
            handle.write(b"out")
        return 0, "", ""

    monkeypatch.setattr(main, "run_snakemake", produce)

    written = []
    original = main.provenance.write
    monkeypatch.setattr(main.provenance, "write",
                        lambda ws, doc: written.append(doc) or original(ws, doc))

    try:
        main.perform_run(json.dumps(WORKFLOW),
                         [("upload", "input-1-out", b"in")])
    finally:
        convert.tools.clear()

    assert len(written) == 1, "a kept workspace should record how it was made"
