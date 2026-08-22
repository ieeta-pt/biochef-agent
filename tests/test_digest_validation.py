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
    source = inspect.getsource(convert.fetch_tool)
    assert "verify_against_manifest" in source
    assert source.index("verify_against_manifest") < source.index("os.replace"), (
        "the check must run while the pull is still staged in .part"
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
        # UnsafeName specifically, not merely "something raised". Without the
        # name check the join still escapes the staging directory, the file is
        # simply not found, and a test that accepted any exception passed while
        # the path traversal went unchecked.
        with pytest.raises(UnsafeName):
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
