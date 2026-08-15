"""Where a run puts its files, and what a failure leaves behind (#40).

Recorded before anything is changed. These assert the CURRENT behaviour, so
they pass against the unfixed service and are the evidence that #40 is a live
defect.

The handler moves the whole process with `os.chdir` (main.py:29) and moves it
back on the last line of the happy path (main.py:65). `os.chdir` is
process-global and this is an async server, so "the current directory" is
shared by everything in flight.

Two of the three failures are deterministic and are tested here. The third --
two requests interleaving -- needs an upload over starlette's 1 MiB spool
threshold to create a suspension point, or more than one worker; it is measured
in the commit message rather than made into a flaky test.
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

import os

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return TestClient(main.app, raise_server_exceptions=False)


def post(client, workflow=b"{}", filename="input-1-out"):
    return client.post(
        "/convert",
        data={"biochef_workflow": workflow},
        files=[("files", (filename, b"payload", "text/plain"))],
    )


def test_a_failed_request_leaves_the_process_in_the_run_directory(client, tmp_path):
    """os.chdir(prev_dir) is not in a finally.

    Anything between the two chdir calls can raise -- a malformed body, a
    registry failure, a missing output -- and the process then stays wherever
    the request left it.
    """
    post(client, workflow=b"this is not json at all")

    assert Path(os.getcwd()) == tmp_path / "tmp", \
        "the process no longer stays in the run directory after a failure"


def test_the_next_request_then_nests_another_level(client, tmp_path):
    """Because the chdir is relative, the damage compounds.

    Each subsequent failure adds a level, and every path the run resolves is
    somewhere the client never named.
    """
    post(client, workflow=b"not json")
    post(client, workflow=b"not json")

    assert (tmp_path / "tmp" / "tmp").is_dir(), "the nesting no longer happens"


def test_a_run_has_no_identity_of_its_own(client, tmp_path):
    """The directory is the constant "tmp", with nothing naming the run.

    So every run writes its inputs, its Snakefile and its outputs to the same
    paths as every other. With more than one worker -- or an upload large enough
    to suspend the handler mid-request -- one run's snakemake reads the
    Snakefile another run wrote. There is no locking either.
    """
    post(client, filename="first-in")

    assert (tmp_path / "tmp" / "first-in").exists(), \
        "the run wrote to a fixed path with no run identifier in it"
    assert not any(p.is_dir() and p.name != "tmp" for p in tmp_path.iterdir()), \
        "nothing per-run was created"
