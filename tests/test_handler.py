"""The /convert handler, end to end through the app.

Every other test on this branch is a unit test of convert.py, so until this file
existed nothing here imported main at all. That mattered more than it sounds:
deleting the one production line #47 changes -- the directory argument passed to
through_intermediate -- left the whole suite green at 76 passed while every
request returned 500. The signature test in test_intermediate_roundtrip.py pins
that the parameter has no default; it cannot pin that the call site passes one.

snakemake is stubbed. These tests are about the handler's own wiring, and
driving a real workflow engine to check an argument is passed would make them
slow and conditional. tests/test_shell_quoting.py runs the real thing.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import convert
import main
from intermediate import SCHEMA_VERSION

BUNDLE = {
    "id": "echoer", "name": "echoer", "bin": "echoer",
    "io": {"inputs": [{"name": "in", "types": ["TEXT"], "mode": "file"}],
           "outputs": [{"name": "out", "types": ["TEXT"], "mode": "stdout"}]},
    "parameters": [],
}

WORKFLOW = {
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Drive the real app with the registry and the engine stubbed out."""
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()

    # Stand in for snakemake by producing the output the rule declares, so the
    # handler's result-collection runs for real.
    def fake_run():
        with open("echoer-1-out", "w") as handle:
            handle.write("PRODUCED")

    monkeypatch.setattr(main, "run_snakemake", fake_run)
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, workflow=None):
    return client.post(
        "/convert",
        data={"biochef_workflow": json.dumps(workflow or WORKFLOW)},
        files=[("files", ("input-1-out", b"hello", "text/plain"))],
    )


def test_a_workflow_converts_and_returns_its_output(client, tmp_path):
    """The whole path: upload, parse, emit, run, collect.

    This is the test whose absence let a broken call site pass 76 tests.
    """
    response = post(client)

    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()["echoer-1"]["out"]) == b"PRODUCED"


def test_the_upload_lands_and_the_snakefile_is_written(client, tmp_path):
    post(client)

    run_dir = tmp_path / "tmp"
    assert (run_dir / "input-1-out").read_bytes() == b"hello"
    assert (run_dir / "Snakefile").exists()


def test_with_the_flag_on_the_document_is_written_beside_the_snakefile(
        client, tmp_path, monkeypatch):
    """#2's acceptance, asserted at the handler rather than at the function.

    The directory the handler passes is the thing under test. A test that calls
    through_intermediate directly proves the function honours its argument; only
    this proves the handler supplies the right one.
    """
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", True)

    response = post(client)

    assert response.status_code == 200, response.text
    run_dir = tmp_path / "tmp"
    document = run_dir / convert.INTERMEDIATE_FILENAME
    assert document.exists(), "the document was not written beside the Snakefile"
    assert json.loads(document.read_text())["schemaVersion"] == SCHEMA_VERSION

    # and not in the process's own directory, which is what a relative default
    # would have produced once the handler stops chdir'ing (#40, #46)
    assert not (tmp_path / convert.INTERMEDIATE_FILENAME).exists()


def test_with_the_flag_off_no_document_is_written(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", False)

    post(client)

    assert not (tmp_path / "tmp" / convert.INTERMEDIATE_FILENAME).exists()


def test_a_malformed_body_does_not_return_a_success(client, tmp_path):
    response = client.post(
        "/convert",
        data={"biochef_workflow": "this is not json at all"},
        files=[("files", ("input-1-out", b"hello", "text/plain"))],
    )

    assert response.status_code >= 400
