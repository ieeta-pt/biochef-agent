"""Which browser origins may call this service (#74)."""

import re
import sys
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
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

import auth
import main
from cors import get_cors_origins, middleware_options

ALLOWED = "http://localhost:3000"
OTHER = "http://elsewhere.example"
TOKEN = "a-shared-secret"


def _app(origins):
    """main's stack, in main's order: body limit, auth, then CORS outermost."""
    import bodylimit

    app = FastAPI()
    for route in main.app.routes:
        if str(getattr(route, "path", "")).startswith("/runs"):
            app.router.routes.append(route)
    app.add_middleware(bodylimit.BodySizeLimitMiddleware)
    app.add_middleware(auth.AuthenticationMiddleware,
                       provider=auth.BearerAuth(TOKEN))
    if origins:
        # The same options main uses, not a copy of them.
        app.add_middleware(CORSMiddleware, **middleware_options(origins))
    return TestClient(app)


def _preflight(client, origin):
    return client.options("/runs/x", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })


# --- the setting -------------------------------------------------------------

def test_unset_means_no_origins():
    assert get_cors_origins("") == []


def test_origins_are_read_exactly():
    assert get_cors_origins(" http://a.example , https://b.example:8443 ") == [
        "http://a.example", "https://b.example:8443"]


def test_a_wildcard_refuses_to_start():
    """This service runs tool binaries; which pages may drive it is named."""
    with pytest.raises(ValueError, match="does not accept"):
        get_cors_origins("*")


@pytest.mark.parametrize("bad", [
    "http://localhost:3000/",          # trailing slash: a browser never sends it
    "http://localhost:3000/editor",    # a path
    "localhost:3000",                  # no scheme
    "ftp://localhost",                 # not http(s)
])
def test_something_that_is_not_an_origin_refuses_to_start(bad):
    """It would look configured and match nothing."""
    with pytest.raises(ValueError, match="not an origin"):
        get_cors_origins(bad)


# --- behaviour ---------------------------------------------------------------

def test_by_default_no_cors_headers_are_sent():
    response = _app([]).get("/runs/x", headers={"Origin": ALLOWED})
    assert "access-control-allow-origin" not in response.headers


def test_a_preflight_is_answered_before_authentication():
    """A preflight carries no Authorization. Beneath authentication it would be
    a 401 and the browser would never send the real request."""
    response = _preflight(_app([ALLOWED]), ALLOWED)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "authorization" in response.headers[
        "access-control-allow-headers"].lower()


def test_a_preflight_from_another_origin_is_refused():
    response = _preflight(_app([ALLOWED]), OTHER)
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_a_401_carries_the_headers_so_the_page_can_read_why():
    response = _app([ALLOWED]).get("/runs/x", headers={"Origin": ALLOWED})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_another_origin_gets_no_headers_on_an_ordinary_response():
    response = _app([ALLOWED]).get(
        "/runs/x", headers={"Origin": OTHER, "Authorization": f"Bearer {TOKEN}"})
    assert "access-control-allow-origin" not in response.headers


def test_credentials_mode_stays_off():
    """Bearer travels in a header; cookies are kept out of it."""
    response = _preflight(_app([ALLOWED]), ALLOWED)
    assert "access-control-allow-credentials" not in response.headers


# --- wiring in main ----------------------------------------------------------

def test_main_adds_cors_after_authentication():
    """Last added is outermost. Pinned on comment-stripped source, because a
    comment mentioning the middleware would otherwise satisfy it."""
    code = re.sub(r"#.*", "", (REPO_ROOT / "main.py").read_text())
    authn = code.index("add_middleware(AuthenticationMiddleware")
    cors = code.index("add_middleware(CORSMiddleware")
    assert authn < cors
    assert "middleware_options(CORS_ORIGINS)" in code


def test_main_only_adds_cors_when_origins_are_configured():
    code = re.sub(r"#.*", "", (REPO_ROOT / "main.py").read_text())
    assert "if CORS_ORIGINS:" in code
