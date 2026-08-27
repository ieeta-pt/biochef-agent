"""An uploaded filename is refused unless it is a plain file name (#39).

The commit before this one recorded what happened previously: `../ESCAPED.txt`
wrote outside the working directory with HTTP 200, an absolute path ignored the
working directory entirely, and both landed even when the request carried
nothing that could be parsed as a workflow -- because the upload loop runs
before `json.loads`. Each test here is the closed form of one of those.

Self-contained on purpose. `convert.py` builds an ORAS client and calls
`login()` at import time, so importing `main` reaches the registry; the stub
below prevents that. It is inline rather than in a conftest because this file
has to work on `master`, where there is no test harness yet -- the harness
arrives separately and the two must not collide.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub the registry before `main` is imported, and only the registry: FastAPI
# has to stay real, because TestClient drives it.
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
from workspace import SAFE_NAME, UnsafeName, check_name


BUNDLE = {"id": "t", "name": "t", "bin": "t",
          "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"},
                     {"name": "second", "types": ["T"], "mode": "file"}],
                 "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
          "parameters": []}

# One declared input, so exactly one upload name is legitimate: "input-1-out".
# An empty workflow expects no uploads at all, which now makes every upload a
# 400 -- correct, but useless for testing the name rule itself.
ONE_INPUT_WORKFLOW = json.dumps({
    "nodes": [
        {"id": "input-1", "type": "inputWorkflowNode",
         "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
        {"id": "t-1", "type": "workflowNode",
         "data": {"label": "t", "repo": "r", "paramValues": {}, "outputs": {}}},
        {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
    ],
    "edges": [
        {"source": "input-1", "sourceHandle": "out", "target": "t-1", "targetHandle": "in"},
        {"source": "t-1", "sourceHandle": "out", "target": "output-1", "targetHandle": "in"},
    ],
}).encode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Drive the handler with its runs rooted under tmp_path.

    The workspace is created under BIOCHEF_RUN_ROOT, so pointing that at
    tmp_path means a name that escaped would land somewhere this test can see.
    snakemake is stubbed out: these tests are about names, and invoking a real
    workflow engine to check one would make them slow and conditional.
    """
    monkeypatch.setattr(main, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path / "cache"))
    os.makedirs(tmp_path / "cache" / "t", exist_ok=True)
    (tmp_path / "cache" / "t" / "t").write_text("#!/bin/sh\n")
    convert.tools.clear()

    def fake_run(ws, *a, **k):
        ws.write_bytes("t-1-out", b"PRODUCED")
        return (0, "", "")

    monkeypatch.setattr(main, "run_snakemake", fake_run)
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, filename, content=b"payload", workflow=ONE_INPUT_WORKFLOW):
    return client.post(
        "/convert",
        data={"biochef_workflow": workflow},
        files=[("files", (filename, content, "text/plain"))],
    )


# --------------------------------------------------------------------------
# the rule


REFUSED = [
    ("../ESCAPED.txt", "parent traversal"),
    ("../../ESCAPED.txt", "further up"),
    ("/etc/PWNED", "absolute, which cwd never constrained"),
    ("sub/../../x", "traversal hidden mid-path"),
    ("sub/x", "a subdirectory is still more than one component"),
    ("..", "the parent itself"),
    (".", "the directory itself"),
    ("", "empty, which resolves to the directory"),
    (".hidden", "a dotfile"),
    ("-rf", "reads as an option to whatever receives it"),
    ("evil-out\n", "a newline, which would add a line to the Snakefile"),
    ("a" * 129, "longer than any generated name"),
    ("café-in", "outside the generated character set"),
    (None, "starlette types filename as str | None"),
]


@pytest.mark.parametrize("name,why", REFUSED, ids=[r[1] for r in REFUSED])
def test_a_name_that_is_not_a_plain_file_name_is_refused(name, why):
    with pytest.raises(UnsafeName):
        check_name(name)


def test_the_names_the_converter_generates_are_accepted():
    """The rule has to admit everything the system actually produces.

    A node id is "{operation.id}-{timestamp}" and a slot is
    f"{source}-{source_handle}", so these are the real shapes.
    """
    for name in ["input-1-out", "tn93.distance-1-out", "Snakefile",
                 "gto.fasta.extract-1730000000000-out", "intermediate.json"]:
        assert check_name(name) == name


def test_the_anchors_reject_a_trailing_newline():
    r"""Why \A and \Z rather than ^ and $.

    With re.match, `$` also matches just before a trailing newline, so the
    obvious spelling of this pattern would accept "evil-out\n" -- and a newline
    is the one character that would let a name write an extra line into the
    generated Snakefile.
    """
    import re
    caret_dollar = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    assert caret_dollar.match("evil-out\n"), "this is the trap being avoided"
    assert not SAFE_NAME.match("evil-out\n")


# --------------------------------------------------------------------------
# through the handler


def test_a_relative_name_no_longer_escapes(client, tmp_path):
    response = post(client, "../ESCAPED.txt")

    assert response.status_code == 400
    assert not list(tmp_path.rglob("ESCAPED.txt"))


def test_an_absolute_name_no_longer_escapes(client, tmp_path):
    target = tmp_path / "elsewhere" / "ABSOLUTE.txt"
    target.parent.mkdir()

    response = post(client, str(target))

    assert response.status_code == 400
    assert not target.exists()


def test_a_malformed_body_is_refused_before_anything_is_written(client, tmp_path):
    """The parse now runs first (#40), so a request that is not a workflow never
    reaches the upload loop at all. The name check still stands on its own --
    see the table above -- but the ordering means it is no longer the only
    thing between a request and a write."""
    response = post(client, "input-1-out", workflow=b"this is not json at all")

    assert response.status_code >= 400
    assert not list(tmp_path.rglob("input-1-out")), "nothing was written"


def test_a_plain_name_still_works(client, tmp_path, monkeypatch):
    """The case that must keep working: the fix cannot be "reject everything"."""
    kept = []
    real = main.make_workspace
    monkeypatch.setattr(main, "make_workspace",
                        lambda root=None: kept.append(real(root)) or kept[-1])
    monkeypatch.setattr(main, "KEEP_WORKSPACE", True)

    response = post(client, "input-1-out")

    assert response.status_code == 200, response.text
    assert (Path(kept[0].path) / "input-1-out").read_bytes() == b"payload"
    kept[0].cleanup()


@pytest.mark.parametrize("name", ["Snakefile", "SNAKEFILE", "snakefile"])
def test_an_upload_named_for_a_file_the_run_creates_is_refused(name, client, tmp_path):
    """Refused as "not an input", earlier than O_EXCL would have caught it.

    "Snakefile" is a perfectly legal single path component, so the shape rule
    passes it. What refuses it is the workflow: the run declares which files it
    consumes, and this is not one of them. That gate fires before anything is
    written, so the generated Snakefile never has to contend for its own slot.

    Case variants matter on macOS, where APFS is case-insensitive by default and
    they would occupy the same slot.
    """
    response = post(client, name)

    assert response.status_code == 400, response.text
    assert "is not an input" in response.text


def test_the_same_input_sent_twice_is_refused(client, tmp_path):
    """O_EXCL, reached with a name the workflow does declare.

    Sending a legitimate input twice is the only way to reach the exclusive-open
    refusal now: anything the run does not expect is turned away by the
    declared-set gate first.
    """
    response = client.post(
        "/convert",
        data={"biochef_workflow": ONE_INPUT_WORKFLOW},
        files=[("files", ("input-1-out", b"first", "text/plain")),
               ("files", ("input-1-out", b"second", "text/plain"))],
    )

    assert response.status_code == 400
    assert "supplied twice" in response.text


def test_a_declared_input_that_never_arrives_is_refused(client, tmp_path):
    """The other half of the declared-set gate.

    A run whose input never arrived used to proceed, generate a Snakefile, and
    fail inside snakemake looking for a file nobody sent. The workflow says what
    it needs, so the request can be refused naming what is missing.

    Two declared inputs, one uploaded.
    """
    two_inputs = json.loads(ONE_INPUT_WORKFLOW)
    two_inputs["nodes"].append(
        {"id": "input-2", "type": "inputWorkflowNode",
         "data": {"outputs": {"out": {"kind": "text", "data": "y"}}}})
    two_inputs["edges"].append(
        {"source": "input-2", "sourceHandle": "out",
         "target": "t-1", "targetHandle": "second"})

    response = client.post(
        "/convert",
        data={"biochef_workflow": json.dumps(two_inputs)},
        files=[("files", ("input-1-out", b"x", "text/plain"))],
    )

    assert response.status_code == 400, response.text
    assert "missing inputs" in response.text
    assert "input-2-out" in response.text
