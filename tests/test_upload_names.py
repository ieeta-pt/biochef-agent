"""What an uploaded filename can do today (#39).

Recorded before anything is changed. These tests exercise the real /convert
handler and assert the CURRENT behaviour, so they pass against the unfixed
service and are the evidence that #39 is a live defect rather than a reading of
the code.

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

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Run the handler somewhere disposable.

    The handler chdirs into ./tmp of wherever the process happens to be, so the
    test has to move the process rather than pass a path. That is itself the
    subject of a separate issue (#40); here it is only scaffolding.
    """
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, filename, content=b"payload", workflow=b"{}"):
    return client.post(
        "/convert",
        data={"biochef_workflow": workflow},
        files=[("files", (filename, content, "text/plain"))],
    )


def test_a_relative_name_escapes_the_working_directory(client, tmp_path):
    """#39, the form the issue leads with."""
    target = tmp_path / "ESCAPED.txt"
    post(client, "../ESCAPED.txt")

    assert target.exists(), "the upload no longer escapes tmp/"
    assert target.read_bytes() == b"payload"


def test_an_absolute_name_ignores_the_working_directory_entirely(client, tmp_path):
    """os.chdir("tmp") is not a boundary against an absolute path."""
    target = tmp_path / "elsewhere" / "ABSOLUTE.txt"
    target.parent.mkdir()
    post(client, str(target))

    assert target.exists()
    assert target.read_bytes() == b"payload"


def test_the_write_happens_before_the_workflow_is_even_parsed(client, tmp_path):
    """The reason this needs no valid workflow, no registry and no snakemake.

    The upload loop is at main.py:32-34 and json.loads is at main.py:37, so a
    request carrying nothing that could be a workflow still lands its file.
    """
    target = tmp_path / "elsewhere" / "NO_WORKFLOW.txt"
    target.parent.mkdir()
    response = post(client, str(target), workflow=b"this is not json at all")

    assert response.status_code == 500, "the request is expected to fail"
    assert target.exists(), "and to have written the file anyway"


def test_a_plain_name_is_written_inside_the_working_directory(client, tmp_path):
    """The case that must keep working after the fix."""
    post(client, "input-1-out")
    assert (tmp_path / "tmp" / "input-1-out").exists()
