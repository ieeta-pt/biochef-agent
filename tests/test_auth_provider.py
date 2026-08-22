"""Who is allowed to make this service run a tool (#10).

Before this, anyone who could reach it. There was no authentication of any kind
-- no dependency on /convert, no header read, no token compared, and no setting
that could switch one on.

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

import pytest

import main


def test_the_service_resolves_a_provider_and_defaults_to_none():
    """Unchanged behaviour by default, but as a named choice rather than a gap.

    C2's acceptance is explicit that provider `none` leaves behaviour unchanged,
    so the default must not start refusing anyone.
    """
    from auth import NoAuth

    assert isinstance(main.AUTH, NoAuth)
    assert "BIOCHEF_AUTH" in Path(REPO_ROOT / "main.py").read_text()


def test_a_request_with_no_credentials_is_served_under_none(monkeypatch):
    """The other half of "unchanged": nothing is refused for lack of a token."""
    from fastapi.testclient import TestClient

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/convert",
        data={"biochef_workflow": '{"nodes": [], "edges": []}'},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))],
    )
    assert response.status_code not in (401, 403), response.status_code


# --------------------------------------------------------------------------
# bearer


def _bearer_app(token="s3cret"):
    from auth import AuthenticationMiddleware, BearerAuth

    return AuthenticationMiddleware(main.app, provider=BearerAuth(token=token))


def test_a_request_without_a_token_is_refused():
    from fastapi.testclient import TestClient

    response = TestClient(_bearer_app(), raise_server_exceptions=False).post(
        "/convert", data={"biochef_workflow": "{}"},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))])

    assert response.status_code == 401
    # RFC 9110 makes this mandatory on a 401, and it is what tells a client
    # which scheme to try.
    assert response.headers.get("www-authenticate") == "Bearer"


def test_the_right_token_is_let_through():
    """The control. Refusing everything would pass every other test here."""
    from fastapi.testclient import TestClient

    response = TestClient(_bearer_app(), raise_server_exceptions=False).post(
        "/convert", headers={"Authorization": "Bearer s3cret"},
        data={"biochef_workflow": "not json"},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))])

    assert response.status_code != 401, response.text


@pytest.mark.parametrize("header", [
    "Bearer wrong",
    "Bearer ",
    "Bearer",
    "Basic s3cret",
    "s3cret",
    "bearer wrong-case-but-wrong-token",
    "Bearer s3cre",          # a prefix of the real token
    "Bearer s3cretx",        # the real token with more after it
])
def test_anything_other_than_the_configured_token_is_refused(header):
    from fastapi.testclient import TestClient

    response = TestClient(_bearer_app(), raise_server_exceptions=False).post(
        "/convert", headers={"Authorization": header},
        data={"biochef_workflow": "{}"},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))])

    assert response.status_code == 401, f"{header!r} was accepted"


def test_a_lowercase_scheme_is_accepted():
    """The scheme is case-insensitive per the specification; the token is not."""
    from fastapi.testclient import TestClient

    response = TestClient(_bearer_app(), raise_server_exceptions=False).post(
        "/convert", headers={"Authorization": "bearer s3cret"},
        data={"biochef_workflow": "not json"},
        files=[("files", ("input-1-out", b"x", "application/octet-stream"))])

    assert response.status_code != 401


def test_the_token_is_compared_in_constant_time():
    """Not ==.

    String comparison returns at the first difference, so how long it takes
    leaks how much of the token was right, and a token can be recovered a
    character at a time. Structural because timing assertions are flaky by
    nature, and the property is "which function is called".
    """
    import auth

    source = inspect.getsource(auth.BearerAuth.authenticate)
    assert "hmac.compare_digest" in source
    assert "== self._token" not in source


def test_bearer_without_a_token_refuses_to_start():
    """Not "starts and rejects everyone", and above all not "accepts everyone".

    An empty configured token compared against an empty presented one would
    admit any caller who sent `Authorization: Bearer` with nothing after it.
    """
    from auth import BearerAuth

    for empty in ("", "   ", None):
        with pytest.raises(ValueError, match="BIOCHEF_AUTH_TOKEN"):
            BearerAuth(token=empty) if empty is not None else BearerAuth(token="")


def test_an_unknown_provider_stops_the_process():
    """A typo in BIOCHEF_AUTH must not quietly leave the service open.

    Falling back to `none` would be the single worst way for this setting to
    fail: the deployment looks configured and is not.
    """
    from auth import get_auth

    with pytest.raises(ValueError) as exc:
        get_auth("passports")
    assert "BIOCHEF_AUTH" in str(exc.value)
    assert "none" in str(exc.value) and "bearer" in str(exc.value)


# --------------------------------------------------------------------------
# where the check happens, which decides what an anonymous caller can cost


def test_an_anonymous_request_is_refused_before_its_body_is_read():
    """A route dependency would run AFTER starlette spooled the whole payload.

    So an unauthenticated caller could still make the service buffer up to
    BIOCHEF_MAX_UPLOAD_BYTES before being told no. Headers are in the ASGI scope
    from the start, so the refusal costs nothing -- provided it sits outside the
    body handling, which is what this pins.
    """
    from fastapi.testclient import TestClient

    consumed = {"chunks": 0}

    def body():
        boundary = "----authtest"
        head = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="files"; '
                'filename="input-1-out"\r\n\r\n').encode()
        yield head
        consumed["chunks"] += 1
        for _ in range(64):
            yield b"x" * 4096
            consumed["chunks"] += 1
        yield f"\r\n--{boundary}--\r\n".encode()

    response = TestClient(_bearer_app(), raise_server_exceptions=False).post(
        "/convert",
        headers={"Content-Type": "multipart/form-data; boundary=----authtest"},
        content=body())

    assert response.status_code == 401
    assert consumed["chunks"] <= 1, (
        f"{consumed['chunks']} chunks were consumed before the refusal; the "
        f"check is running after the body has been read"
    )


def test_authentication_wraps_the_body_limit_not_the_other_way_round():
    """Order is the whole point, so it is asserted rather than assumed."""
    names = [m.cls.__name__ for m in main.app.user_middleware if hasattr(m, "cls")]
    assert "AuthenticationMiddleware" in names
    assert "BodySizeLimitMiddleware" in names
    # starlette applies user_middleware outermost-first in this list.
    assert names.index("AuthenticationMiddleware") < names.index("BodySizeLimitMiddleware"), (
        f"middleware order is {names}; authentication must be outermost"
    )
