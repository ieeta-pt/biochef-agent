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
from workspace import make_workspace


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


def test_this_repository_does_not_verify_either():
    source = inspect.getsource(convert.fetch_tool)
    assert "client.pull" in source
    # Not "digest": the function already carries a comment saying this is where
    # a digest would be checked, and matching on prose rather than on code is
    # how a test ends up asserting about a comment.
    for evidence_of_checking in ("sha256", "hashlib", "get_manifest",
                                 "hexdigest"):
        assert evidence_of_checking not in source, evidence_of_checking


def test_a_tampered_bundle_is_accepted_and_becomes_an_executable(tmp_path, monkeypatch):
    """The consequence, end to end.

    A registry -- or anything able to answer for one -- returns bytes that are
    not the artifact. Nothing compares them to anything, the bundle is cached as
    though it were genuine, and materialise_tools places the binary in the run
    directory with the execute bit set.
    """
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    convert.tools.clear()

    class _TamperingRegistry:
        """Answers with content that matches no digest at all."""

        def pull(self, target, outdir):
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, "bundle.json"), "w") as f:
                json.dump({"id": "tool", "name": "tool", "bin": "tool",
                           "io": {"inputs": [], "outputs": []},
                           "parameters": []}, f)
            with open(os.path.join(outdir, "tool"), "wb") as f:
                f.write(b"#!/bin/sh\necho not-the-tool-you-asked-for\n")

    monkeypatch.setattr(convert, "client", _TamperingRegistry())

    bundle = convert.fetch_tool("tool-1", "tool:latest")
    assert bundle["bin"] == "tool", "the tampered bundle was accepted"

    workflow = convert.Workflow(nodes=[convert.Node(id="tool-1", bin="tool")])
    ws = make_workspace(str(tmp_path / "runs"))
    try:
        convert.materialise_tools(workflow, ws)
        placed = Path(ws.path) / "tool"
        assert placed.exists()
        assert b"not-the-tool-you-asked-for" in placed.read_bytes()
        assert os.stat(placed).st_mode & 0o100, (
            "content nobody vouched for is now executable in the run directory"
        )
    finally:
        ws.cleanup()
        convert.tools.clear()


def test_nothing_ever_asks_the_registry_for_a_manifest():
    """Which is where the digests would come from.

    fetch_tool calls pull and nothing else, so the manifest -- the only
    statement of what the bytes should be -- is never fetched by this code.
    """
    source = Path(REPO_ROOT / "convert.py").read_text()
    assert "get_manifest" not in source
