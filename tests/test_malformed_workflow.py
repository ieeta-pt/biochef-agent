"""A workflow the client got wrong is the client's error (#86).

Measured before the change: nine of ten malformed documents made `/convert`
answer 500, and `/runs` recorded five of six as SYSTEM_ERROR -- which in WES
means this service failed -- carrying the Python exception's own text, so a
caller was told `'id'` or `'int' object is not subscriptable`.

Both are wrong in the same way. The agent took the blame for what was sent to
it, and anyone triaging a federated run would have looked for a fault here.
WES already has the distinction: EXECUTOR_ERROR is the submission or its tools,
SYSTEM_ERROR is us.
"""

import json
import sys
import time
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

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import pytest
from fastapi.testclient import TestClient

import convert
import main
from runs import TERMINAL

# Every shape the parser would have tripped over, named by what the caller did.
MALFORMED = {
    "not json at all": "x",
    "json but an array": "[]",
    "json but a string": '"hello"',
    "json null": "null",
    "json but a number": "7",
    "no nodes": '{"edges": []}',
    "no edges": '{"nodes": []}',
    "nodes is a number": '{"nodes": 5, "edges": []}',
    "edges is an object": '{"nodes": [], "edges": {}}',
    "a node is a string": '{"nodes": ["x"], "edges": []}',
    "a node is null": '{"nodes": [null], "edges": []}',
    "a node has no id": '{"nodes": [{"type": "workflowNode"}], "edges": []}',
    "a node has no type": '{"nodes": [{"id": "t-1"}], "edges": []}',
    "an id is a number": '{"nodes": [{"id": 7, "type": "workflowNode"}], "edges": []}',
    "a tool node has no data":
        '{"nodes": [{"id": "t-1", "type": "workflowNode"}], "edges": []}',
    "data is a list":
        '{"nodes": [{"id": "t-1", "type": "workflowNode", "data": []}], "edges": []}',
    "repo is missing":
        '{"nodes": [{"id": "t-1", "type": "workflowNode", "data": {}}], "edges": []}',
    "repo is a number":
        '{"nodes": [{"id": "t-1", "type": "workflowNode", "data": {"repo": 7}}],'
        ' "edges": []}',
    "repo is empty":
        '{"nodes": [{"id": "t-1", "type": "workflowNode", "data": {"repo": ""}}],'
        ' "edges": []}',
    "paramValues is a list":
        '{"nodes": [{"id": "t-1", "type": "workflowNode",'
        ' "data": {"repo": "r", "paramValues": []}}], "edges": []}',
    "a parameter is a string":
        '{"nodes": [{"id": "t-1", "type": "workflowNode",'
        ' "data": {"repo": "r", "paramValues": {"k": "v"}}}], "edges": []}',
    "an enabled parameter has no value":
        '{"nodes": [{"id": "t-1", "type": "workflowNode",'
        ' "data": {"repo": "r", "paramValues": {"k": {"enabled": true}}}}],'
        ' "edges": []}',
    "an edge is a number": '{"nodes": [], "edges": [3]}',
    "an edge has no source": '{"nodes": [], "edges": [{"target": "x"}]}',
    "an edge has no target": '{"nodes": [], "edges": [{"source": "x"}]}',
    "a source is a number":
        '{"nodes": [], "edges": [{"source": 1, "target": "x"}]}',
}

UPLOAD = [("files", ("input-1-out", b"in", "application/octet-stream"))]


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as made:
        yield made


# --- the synchronous route -------------------------------------------------

@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_convert_refuses_rather_than_failing(client, label):
    response = client.post("/convert",
                           data={"biochef_workflow": MALFORMED[label]},
                           files=UPLOAD)
    assert response.status_code == 400, (
        f"{label}: answered {response.status_code}, so this service is still "
        f"reporting the caller's mistake as its own"
    )


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_the_refusal_says_what_was_wrong(client, label):
    """A 400 with nothing in it is only slightly better than a 500."""
    detail = client.post("/convert",
                         data={"biochef_workflow": MALFORMED[label]},
                         files=UPLOAD).json()["detail"]
    assert isinstance(detail, str) and len(detail) > 20, detail
    assert "biochef_workflow" in detail or "node" in detail or "edge" in detail


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_the_refusal_is_not_a_python_exception(client, label):
    """What a caller used to get was `'id'`, or `'int' object is not
    subscriptable`. Those name our types and our variables, and tell a caller
    nothing they can act on."""
    detail = client.post("/convert",
                         data={"biochef_workflow": MALFORMED[label]},
                         files=UPLOAD).json()["detail"]
    for leak in ("Traceback", "KeyError", "TypeError", "object is not",
                 "NoneType", "subscriptable", "has no attribute"):
        assert leak not in detail, f"{label}: the refusal leaks {leak!r}"


# --- the asynchronous route ------------------------------------------------

@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_runs_blames_the_submission_and_not_the_service(client, label):
    """EXECUTOR_ERROR, not SYSTEM_ERROR. A hub reading a trail of federated runs
    sorts them by exactly this, and five of these used to say the agent broke.
    """
    submitted = client.post("/runs",
                            data={"biochef_workflow": MALFORMED[label]},
                            files=UPLOAD)
    assert submitted.status_code == 202
    run_id = submitted.json()["run_id"]

    for _ in range(500):
        body = client.get(f"/runs/{run_id}").json()
        if body["state"] in {state.value for state in TERMINAL}:
            break
        time.sleep(0.02)

    assert body["state"] == "EXECUTOR_ERROR", (
        f"{label}: settled {body['state']}, which says this service failed"
    )


# --- the guard that matters most -------------------------------------------

def test_a_bug_of_ours_is_still_a_500(client, monkeypatch):
    """The reason this validates explicitly instead of wrapping the parse.

    A try/except around parse_biochef_workflow catching KeyError and TypeError
    would need no knowledge of the document and would cover every field for
    free -- and would also turn a genuine fault in our own parsing into a tidy
    400, which is the `except Exception` mistake wearing different clothes. A
    caller would be told their workflow was wrong when it was not, and nothing
    would record that this service had broken.
    """
    def exploding(document):
        raise TypeError("a bug of ours, not of the caller's")

    monkeypatch.setattr(main, "parse_biochef_workflow", exploding)

    good = json.dumps({"nodes": [], "edges": []})
    response = client.post("/convert", data={"biochef_workflow": good},
                           files=UPLOAD)
    assert response.status_code == 500, (
        "an internal fault came back as a refusal, so it would never be found"
    )


def test_a_valid_document_still_gets_through():
    """The validator is a shape check and must not reject a real workflow.

    Driven against the document the rest of the suite uses, so this fails if a
    check here is stricter than what the editor actually sends.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_async_runs import WORKFLOW

    document = convert.read_workflow_document(WORKFLOW)
    assert document["nodes"] and document["edges"]


def test_the_shape_check_runs_before_a_workspace_is_made():
    """Not for speed -- a refused submission should leave nothing behind.

    Validating after make_workspace would create and then remove a directory
    for every malformed document, which is work and a window in which a run
    directory exists for a run that never was.
    """
    import inspect

    source = inspect.getsource(main.perform_run)
    stripped = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
    assert ("read_workflow_document" in stripped
            and "make_workspace" in stripped)
    assert stripped.index("read_workflow_document") \
        < stripped.index("make_workspace"), (
        "the document is parsed after the workspace is created, so a malformed "
        "submission makes a directory first"
    )


# ---------------------------------------------------------------------------
# naming something the tool does not declare
#
# read_workflow_document cannot catch these: it runs before the bundle is
# pulled, so it does not know which handles or parameters a tool has. They
# were three bare next(...) calls with no default, which raised StopIteration
# from the middle of a run -- 500 on /convert, and SYSTEM_ERROR carrying
# "coroutine raised StopIteration" on /runs. The same mistake this file is
# about, in the one place its own check cannot reach.

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_async_runs import WORKFLOW, service   # noqa: E402


def _naming(**change):
    document = json.loads(WORKFLOW)
    if "parameter" in change:
        for node in document["nodes"]:
            if node["id"] == "tool-1":
                node["data"]["paramValues"] = {
                    change["parameter"]: {"enabled": True, "value": "x"}}
    if "target_handle" in change:
        document["edges"][0]["targetHandle"] = change["target_handle"]
    if "source_handle" in change:
        document["edges"][1]["sourceHandle"] = change["source_handle"]
    return json.dumps(document)


@pytest.mark.parametrize("label, document, named", [
    ("a parameter the tool does not declare",
     _naming(parameter="not-a-real-flag"), "not-a-real-flag"),
    ("an input handle the tool does not have",
     _naming(target_handle="no-such-input"), "no-such-input"),
    ("an output handle the tool does not have",
     _naming(source_handle="no-such-output"), "no-such-output"),
])
def test_naming_what_the_tool_lacks_is_refused(service, label, document, named):
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as client:
        response = client.post("/convert",
                               data={"biochef_workflow": document},
                               files=UPLOAD)

    assert response.status_code == 400, f"{label}: {response.status_code}"
    detail = response.json()["detail"]
    assert named in detail, detail
    assert "does not declare" in detail


@pytest.mark.parametrize("label, document", [
    ("parameter", _naming(parameter="not-a-real-flag")),
    ("input handle", _naming(target_handle="no-such-input")),
    ("output handle", _naming(source_handle="no-such-output")),
])
def test_the_refusal_says_what_the_tool_does_declare(service, label, document):
    """So a caller can correct it rather than guess.

    The tool's declared interface is not ours to withhold -- the editor
    already holds the bundle it came from.
    """
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as client:
        detail = client.post("/convert", data={"biochef_workflow": document},
                             files=UPLOAD).json()["detail"]

    assert "it has [" in detail, detail
    for leak in ("StopIteration", "Traceback", "coroutine"):
        assert leak not in detail


def test_runs_calls_these_the_submissions_fault_too(service):
    """EXECUTOR_ERROR, not SYSTEM_ERROR carrying our own exception's text."""
    import time

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        submitted = client.post(
            "/runs",
            data={"biochef_workflow": _naming(parameter="not-a-real-flag")},
            files=UPLOAD)
        assert submitted.status_code == 202
        run_id = submitted.json()["run_id"]
        for _ in range(500):
            body = client.get(f"/runs/{run_id}").json()
            if body["state"] in {state.value for state in TERMINAL}:
                break
            time.sleep(0.02)

    assert body["state"] == "EXECUTOR_ERROR", body["state"]
    assert "StopIteration" not in str(body.get("error"))


def test_a_valid_workflow_is_unaffected(service):
    """The lookup refuses what is absent and must not refuse what is there."""
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as client:
        response = client.post("/convert", data={"biochef_workflow": WORKFLOW},
                               files=UPLOAD)

    assert response.status_code == 200, response.text
    assert response.json()["tool-1"]["out"]
