"""Who is allowed to make this service run a tool (#10).

Recorded before anything changes. Anyone who can reach it.

There is no authentication of any kind: no dependency on /convert, no header
read, no token compared, nothing in the settings that could switch one on. The
endpoint accepts a workflow, pulls binaries from a registry, executes them, and
returns their output, to any caller that can open a socket to it.

That is a defensible default for something on a laptop. It is the wrong one for
a service whose reason to exist is dispatching work into a Trusted Research
Environment, where the whole point is that not everyone may ask.

C2 asks for the interface plus two providers, so that F3 (Passports) is a third
provider rather than a rewrite.
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

import main


def test_the_run_endpoint_has_no_dependencies_at_all():
    """Which is where an authentication check would live."""
    convert_route = next(r for r in main.app.routes
                         if getattr(r, "path", None) == "/convert")
    assert convert_route.dependant.dependencies == []


def test_nothing_in_the_service_reads_an_authorization_header():
    source = Path(REPO_ROOT / "main.py").read_text()
    for evidence in ("Authorization", "HTTPBearer", "APIKeyHeader", "Security",
                     "credentials", "401"):
        assert evidence not in source, evidence


def test_no_setting_could_switch_authentication_on():
    """Every other boundary in this service is configurable. This one is absent.

    BIOCHEF_RUNNER chooses how a workflow executes, BIOCHEF_MAX_UPLOAD_BYTES
    bounds what may be sent, BIOCHEF_APPTAINER_ARGS decides what a step can see.
    There is no equivalent for who may ask in the first place.
    """
    source = Path(REPO_ROOT / "main.py").read_text()
    assert "BIOCHEF_RUNNER" in source
    assert "BIOCHEF_AUTH" not in source
    assert not (REPO_ROOT / "auth.py").exists()


def test_a_request_with_no_credentials_is_served(monkeypatch, tmp_path):
    """The consequence, stated plainly.

    A bad request is refused for being a bad request -- never for being an
    unauthenticated one. Anything other than 401/403 here means the caller's
    identity was never consulted.
    """
    from fastapi.testclient import TestClient

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/convert",
        data={"biochef_workflow": '{"nodes": [], "edges": []}'},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))],
    )

    assert response.status_code not in (401, 403), (
        f"got {response.status_code}; the request was judged on its contents "
        f"alone, with no credentials presented"
    )
