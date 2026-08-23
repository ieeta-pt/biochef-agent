"""What checks a tool binary before we execute it (#9).

Recorded before anything changes. Nothing does.

C1 asks whether downloaded blobs are checked against the manifest digest
anywhere today. They are not, in this repository or in the library it delegates
to. oras builds the blob URL from the digest and then streams the response
straight to a file:

    with self.get_blob(container, digest, stream=True) as r:
        with open(outfile, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

There is a get_file_hash in oras.utils, used on the push path. Nothing on the
pull path calls it. So the digest names what we asked for, never what we got.

What arrives is then chmod 0700 and executed. Between "the registry holds a
correct artifact" and "we run the right bytes" sits every hop in between, and at
present nothing closes that gap -- which is the whole of C1, and the reason E1
(cosign) has nothing to stand on yet.
"""

import hashlib
import inspect
import json
import os
import sys
import sysconfig
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras_mod = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import pytest

import convert
from workspace import UnsafeName, make_workspace


def _oras_provider_source():
    """The installed oras/provider.py, read as text.

    Read rather than imported. Every test module here installs a stub in
    sys.modules so nothing reaches the registry, and in a full run some other
    module gets there first -- so `import oras.provider` finds the stub, which
    is not a package, and the test that matters either explodes or skips. Both
    outcomes look like the check ran. Reading the file cannot be shadowed.
    """
    for base in {sysconfig.get_paths()["purelib"],
                 sysconfig.get_paths()["platlib"]}:
        candidate = Path(base) / "oras" / "provider.py"
        if candidate.exists():
            return candidate.read_text()
    return None


def test_the_library_does_not_verify_what_it_downloads():
    """Read from the installed oras, not asserted about it.

    Skipped rather than guessed at if the real package is not installed, because
    a stub would make this pass while proving nothing.
    """
    source = _oras_provider_source()
    if source is None:
        pytest.skip("the installed oras package could not be located on disk")

    download = source.split("def download_blob")[1].split("\n    def ")[0]
    assert "get_blob" in download, "read the wrong function"
    for verifying in ("hashlib", "get_file_hash", "hexdigest"):
        assert verifying not in download, (
            f"oras now does {verifying} on the pull path -- if so this issue is "
            f"moot and this test should say so"
        )

    pull = source.split("    def pull(")[1].split("\n    @")[0]
    assert "download_blob" in pull
    assert "get_file_hash" not in pull


def test_the_verification_happens_before_the_bundle_is_promoted():
    """Order matters more than presence.

    Verifying after os.replace would leave a window in which a tampered bundle
    is the cached one, and the cache is what every later run copies from.
    """
    # _fetch_tool_once, not fetch_tool: the latter is now a thin retry wrapper
    # around it, for a cross-process race in the shared cache.
    source = inspect.getsource(convert._fetch_tool_once)
    promote = inspect.getsource(convert._promote)

    assert "verify_against_manifest" in source
    # The CALL, not the string. Asserting on "os.replace" matched a comment
    # explaining the promote, which is the second time in this suite that a
    # check has been fooled by prose sitting next to the code it describes.
    assert "os.replace(staging, outdir)" not in source, (
        "the promote moved into _promote; this test must follow it"
    )
    assert "os.replace(staging, outdir)" in promote
    assert source.index("verify_against_manifest") < source.index("_promote("), (
        "the check must run while the pull is still staged, before anything is "
        "put in place"
    )


def _manifest_for(files):
    return {"layers": [
        {"digest": "sha256:" + hashlib.sha256(content).hexdigest(),
         "mediaType": "application/octet-stream",
         "annotations": {"org.opencontainers.image.title": name}}
        for name, content in files.items()
    ]}


class _Registry:
    """A registry that pulls one set of bytes and vouches for another."""

    def __init__(self, written, vouched=None):
        self.written = written
        self.vouched = written if vouched is None else vouched
        self.manifest = _manifest_for(self.vouched)

    def get_container(self, target):
        return target

    def get_manifest(self, container, *a, **k):
        return self.manifest

    def pull(self, target, outdir):
        os.makedirs(outdir, exist_ok=True)
        for name, content in self.written.items():
            with open(os.path.join(outdir, name), "wb") as f:
                f.write(content)


GENUINE = {"bundle.json": json.dumps(
               {"id": "tool", "name": "tool", "bin": "tool",
                "io": {"inputs": [], "outputs": []}, "parameters": []}
           ).encode(),
           "tool": b"#!/bin/sh\necho genuine\n"}


def test_a_bundle_that_matches_its_manifest_is_accepted(tmp_path, monkeypatch):
    """The control. Without it, refusing everything would pass every other test."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry(GENUINE))
    convert.tools.clear()
    try:
        assert convert.fetch_tool("tool-1", "tool:latest")["bin"] == "tool"
    finally:
        convert.tools.clear()


def test_a_tampered_blob_is_rejected(tmp_path, monkeypatch):
    """C1's acceptance: a deliberately tampered blob, refused explicitly."""
    tampered = dict(GENUINE)
    tampered["tool"] = b"#!/bin/sh\necho not-the-tool-you-asked-for\n"

    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry(tampered, vouched=GENUINE))
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError) as exc:
            convert.fetch_tool("tool-1", "tool:latest")
        assert "does not match the manifest" in str(exc.value)
        assert "tool" in str(exc.value)
    finally:
        convert.tools.clear()


def test_a_rejected_bundle_is_not_left_anywhere_a_run_could_use_it(tmp_path, monkeypatch):
    """Refusing is not enough if the bytes stay on disk.

    The cached directory is what materialise_tools copies into a run, so a
    tampered pull must leave neither a promoted bundle nor a .part beside it.
    """
    tampered = dict(GENUINE)
    tampered["tool"] = b"tampered"

    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    monkeypatch.setattr(convert, "client", _Registry(tampered, vouched=GENUINE))
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError):
            convert.fetch_tool("tool-1", "tool:latest")
        assert not (cache / "tool").exists(), "a tampered bundle was promoted"
        assert not (cache / "tool.part").exists(), "the staged copy was left behind"
        assert "tool" not in convert.tools, "the tampered bundle was memoised"
    finally:
        convert.tools.clear()


def test_a_directory_layer_is_refused_rather_than_skipped(tmp_path, monkeypatch):
    """It cannot be verified, so it must not be treated as verified.

    oras downloads a directory layer to a temporary archive and extracts it, so
    nothing on disk is the blob. Passing it silently would be the exact failure
    this issue is about, dressed up as a check.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    registry.manifest["layers"][1]["mediaType"] = (
        "application/vnd.oci.image.layer.v1.tar+gzip")
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="directory archive"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()


def test_a_manifest_title_is_not_allowed_to_choose_a_path(tmp_path, monkeypatch):
    """The manifest comes from the registry, and the client chooses the registry.

    A title of "../../etc/cron.d/x" would otherwise decide which file gets
    hashed, and by extension which path is consulted at all.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    registry.manifest["layers"][1]["annotations"][
        "org.opencontainers.image.title"] = "../../etc/passwd"
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        # The containment error specifically, not merely "something raised".
        # Without the check the join still escapes the staging directory, the
        # file is simply not found, and a test that accepted any exception
        # passed while the traversal went unchecked.
        with pytest.raises(convert.ToolIntegrityError, match="resolves outside"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()


def test_a_manifest_with_no_layers_is_refused(tmp_path, monkeypatch):
    """Otherwise "verified" would mean "there was nothing to check"."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    registry.manifest = {"layers": []}
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="no layers"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()


def test_a_declared_file_that_never_arrived_is_refused(tmp_path, monkeypatch):
    """A manifest naming a file the pull did not produce is not a pass."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    vouched = dict(GENUINE)
    vouched["extra"] = b"never written"
    monkeypatch.setattr(convert, "client", _Registry(GENUINE, vouched=vouched))
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="did not"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()


def test_the_manifest_is_what_the_digests_come_from():
    """Not a digest the caller passed in, which would prove nothing.

    The manifest is fetched from the registry for the same target that was
    pulled, so the comparison is against the registry's own statement of what
    the artifact is.
    """
    source = inspect.getsource(convert.verify_against_manifest)
    assert "client.get_manifest" in source
    assert "hashlib" in inspect.getsource(convert._sha256_of)


# --------------------------------------------------------------------------
# the cache is what actually runs, so it is what has to be verified


def test_a_nested_layer_title_is_accepted(tmp_path, monkeypatch):
    """oras accepts titles like "bin/tool" and sanitises them.

    The first version of this used check_name, which admits one plain file name,
    so it refused "bin/tool" and "lib/libz.so.1" -- a bundle laid out in
    subdirectories would have been rejected as an integrity failure. Containment
    is the property that matters, not flatness.
    """
    nested = {"bundle.json": GENUINE["bundle.json"],
              "bin/tool": b"#!/bin/sh\necho nested\n"}

    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    class _Nested(_Registry):
        def pull(self, target, outdir):
            for name, content in self.written.items():
                path = os.path.join(outdir, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(content)

    monkeypatch.setattr(convert, "client", _Nested(nested))
    convert.tools.clear()
    try:
        assert convert.fetch_tool("tool-1", "tool:latest")["bin"] == "tool"
    finally:
        convert.tools.clear()


def test_a_cached_bundle_is_verified_again_before_it_is_used(tmp_path, monkeypatch):
    """The cache is what runs, so verifying only the pull proves nothing.

    TOOL_CACHE is a plain directory this service writes, and materialise_tools
    copies out of it and chmods 0700. A tool running under the subprocess runner
    is unconfined and runs as the same user, so one run can rewrite what every
    later run executes -- no cleverness required, just a shared cache.

    Verifying only at pull time made that invisible: the second fetch asked the
    registry nothing at all and handed back the rewritten bytes.
    """
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    registry = _Registry(GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        assert (cache / "tool" / "tool").read_bytes() == GENUINE["tool"]

        # Something with the agent's own privileges rewrites the cached binary.
        (cache / "tool" / "tool").write_bytes(b"#!/bin/sh\necho rewritten\n")
        convert.tools.clear()          # as if a later request, or a restart

        convert.fetch_tool("tool-1", "tool:latest")
        assert (cache / "tool" / "tool").read_bytes() == GENUINE["tool"], (
            "the rewritten binary survived and would have been executed"
        )
    finally:
        convert.tools.clear()


def test_the_registry_is_consulted_on_every_fetch(tmp_path, monkeypatch):
    """Not only on a cache miss, and not skipped by the in-process memo.

    The memo returned the parsed bundle without looking at disk, so a cached
    entry short-circuited the check within a process as well as across restarts.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))

    class _Counting(_Registry):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.manifest_fetches = 0

        def get_manifest(self, container, *a, **k):
            self.manifest_fetches += 1
            return self.manifest

    registry = _Counting(GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        convert.fetch_tool("tool-1", "tool:latest")
        assert registry.manifest_fetches == 2, (
            f"the second fetch consulted the registry {registry.manifest_fetches} "
            f"time(s); a cached entry must still be checked"
        )
    finally:
        convert.tools.clear()


def test_a_moved_tag_replaces_the_cache_rather_than_failing(tmp_path, monkeypatch):
    """A new version published under the same tag is ordinary, not an incident.

    The cache check is silent for exactly this reason: a mismatch there means
    pull again, and only a mismatch AFTER the pull is an integrity failure.
    """
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    registry = _Registry(GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")

        # The tag moves: new bytes, and a manifest that vouches for them.
        newer = dict(GENUINE)
        newer["tool"] = b"#!/bin/sh\necho version-two\n"
        registry.written = newer
        registry.vouched = newer
        registry.manifest = _manifest_for(newer)
        convert.tools.clear()

        convert.fetch_tool("tool-1", "tool:latest")
        assert (cache / "tool" / "tool").read_bytes() == newer["tool"]
    finally:
        convert.tools.clear()


def test_a_client_is_told_the_failure_was_upstream(tmp_path, monkeypatch):
    """502 with a reason, not a bare 500.

    Unhandled, ToolIntegrityError surfaced as "Internal Server Error" -- which
    leaked nothing, but also said nothing, and gave an operator no reason to
    look at the registry. The failure is upstream and not the client's doing.
    """
    import main
    from fastapi.testclient import TestClient

    tampered = dict(GENUINE)
    tampered["tool"] = b"TAMPERED"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry(tampered, vouched=GENUINE))
    convert.tools.clear()

    workflow = json.dumps({
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode", "data": {}},
            {"id": "tool-1", "type": "workflowNode",
             "data": {"label": "tool", "repo": "r", "paramValues": {}, "outputs": {}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": "tool-1", "targetHandle": "in"},
            {"source": "tool-1", "sourceHandle": "out",
             "target": "output-1", "targetHandle": "in"},
        ],
    })

    try:
        client = TestClient(main.app, raise_server_exceptions=False)
        response = client.post(
            "/convert", data={"biochef_workflow": workflow},
            files=[("files", ("input-1-out", b"data", "application/octet-stream"))])

        assert response.status_code == 502, response.status_code
        assert "tool_integrity" in response.text
        assert "does not match the manifest" in response.text
        # Nothing local: the message is about the artifact, not this machine.
        assert str(tmp_path) not in response.text
        assert "Traceback" not in response.text
    finally:
        convert.tools.clear()
