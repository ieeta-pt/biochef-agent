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

    assert source.index("verify_against_manifest(pull_target") < source.index(
        "os.replace(staging, outdir)"), (
        "the check must run while the pull is still staged, before anything is "
        "put in place"
    )
    assert source.index("evidence_verification.verify_pulled(") < source.index(
        "os.replace(staging, outdir)"), (
        "signed evidence and pulled bytes must be checked before promotion"
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
        self.pulled = False
        self.requested = []

    def get_container(self, target):
        return target

    def get_manifest(self, container, *a, **k):
        return self.manifest

    def download_blob(self, container, digest, outfile):
        """Writes what this registry serves under the title the agent asked for."""
        self.pulled = True
        self.requested.append(digest)
        for name, content in self.written.items():
            if outfile.endswith(os.sep + name):
                with open(outfile, "wb") as f:
                    f.write(content)
                return outfile
        # What oras does when the blob is the empty one and the request fails:
        # it writes nothing and returns os.devnull.
        return os.devnull


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
        assert not registry.pulled, "oras extracts it during the pull"
    finally:
        convert.tools.clear()


def test_a_layer_without_a_title_is_refused(tmp_path, monkeypatch):
    """Its hash would match, which is why hashing alone never caught it.

    oras saves an untitled layer under its digest, so the staged file is exactly
    the blob the manifest names. The bundle's own files are all still present,
    so the image is a genuine bundle with something added to it.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    added = b"\x1f\x8b an added layer"
    digest = "sha256:" + hashlib.sha256(added).hexdigest()
    registry.written = {**GENUINE, digest: added}
    registry.manifest["layers"].append({
        "digest": digest,
        "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
    })
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="has no title"):
            convert.fetch_tool("tool-1", "tool:latest")
        assert not registry.pulled, "the pull must not start"
        assert not os.path.exists(tmp_path / "cache" / "tool")
    finally:
        convert.tools.clear()


def test_a_cached_bundle_is_not_trusted_once_its_manifest_gains_a_layer(
        tmp_path, monkeypatch):
    """The cache is compared with the manifest, so it must refuse the same way."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        registry.manifest["layers"].append({
            "digest": "sha256:" + "0" * 64,
            "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
            "annotations": None,
        })
        with pytest.raises(convert.ToolIntegrityError, match="has no title"):
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

    monkeypatch.setattr(convert, "client", _Registry(nested))
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


# --------------------------------------------------------------------------
# the blobs come from the manifest that was checked, and nothing else stays


def test_the_blobs_are_the_ones_the_checked_manifest_names(tmp_path, monkeypatch):
    """A registry that answers differently the second time gets no second ask.

    client.pull fetched the manifest again and downloaded what that copy named,
    so every check before it covered only the first copy. This registry serves
    an extra file in every manifest after the first, and offers it for download.
    """
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))

    class _Changing(_Registry):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.manifest_fetches = 0
            self.later = _manifest_for({**GENUINE, "extra": b"added later"})

        def get_manifest(self, container, *a, **k):
            self.manifest_fetches += 1
            return self.manifest if self.manifest_fetches == 1 else self.later

        def pull(self, target, outdir):
            raise AssertionError("client.pull fetches the manifest again")

    registry = _Changing({**GENUINE, "extra": b"added later"}, vouched=GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()

    assert registry.manifest_fetches == 1
    assert sorted(registry.requested) == sorted(
        layer["digest"] for layer in registry.manifest["layers"])
    assert sorted(os.listdir(cache / "tool")) == sorted(GENUINE)


def test_a_title_used_twice_is_refused(tmp_path, monkeypatch):
    """The second file would replace the first, and only one digest could match."""
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    registry = _Registry(GENUINE)
    registry.manifest["layers"].append({
        "digest": "sha256:" + hashlib.sha256(b"other").hexdigest(),
        "mediaType": "application/octet-stream",
        "annotations": {"org.opencontainers.image.title": "tool"},
    })
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="twice"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()
    assert not os.path.exists(tmp_path / "cache" / "tool")
    assert not list((tmp_path / "cache").glob("tool.part.*"))


def test_a_file_named_as_the_directory_of_another_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(convert, "client", _Registry(
        {**GENUINE, "bin": b"a file", "bin/tool": b"under it"}))
    convert.tools.clear()
    try:
        with pytest.raises(convert.ToolIntegrityError, match="bin"):
            convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()
    assert not os.path.exists(tmp_path / "cache" / "tool")


def test_a_cached_bundle_with_a_file_the_manifest_does_not_name_is_pulled_again(
        tmp_path, monkeypatch):
    """Every named file matching is not enough: the extra one would stay beside the tool."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    registry = _Registry(GENUINE)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        (cache / "tool" / "extra").write_bytes(b"left in the cache")
        convert.tools.clear()
        registry.requested.clear()

        convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()

    assert registry.requested, "the cache was used as it was"
    assert sorted(os.listdir(cache / "tool")) == sorted(GENUINE)


def test_a_named_file_that_is_a_symlink_in_the_cache_is_pulled_again(
        tmp_path, monkeypatch):
    """A link to another named file with the same bytes passes every hash.

    It stays inside the bundle's directory and names nothing unnamed, so only
    refusing links catches it.
    """
    files = {**GENUINE, "tool-copy": GENUINE["tool"]}
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    registry = _Registry(files)
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        os.remove(cache / "tool" / "tool-copy")
        os.symlink("tool", cache / "tool" / "tool-copy")
        convert.tools.clear()
        registry.requested.clear()

        convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()

    assert registry.requested, "the symlink was trusted"
    assert not os.path.islink(cache / "tool" / "tool-copy")


def test_a_cached_bundle_with_a_directory_the_manifest_does_not_name_is_pulled_again(
        tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(convert, "TOOL_CACHE", str(cache))
    registry = _Registry({"bundle.json": GENUINE["bundle.json"],
                          "bin/tool": GENUINE["tool"]})
    monkeypatch.setattr(convert, "client", registry)
    convert.tools.clear()
    try:
        convert.fetch_tool("tool-1", "tool:latest")
        (cache / "tool" / "lib").mkdir()
        convert.tools.clear()
        registry.requested.clear()

        convert.fetch_tool("tool-1", "tool:latest")
    finally:
        convert.tools.clear()

    assert registry.requested, "the cache was used as it was"
    assert sorted(os.listdir(cache / "tool")) == ["bin", "bundle.json"]
