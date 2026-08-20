"""What bounds the size of a request today (#11).

Recorded before anything changes. Nothing does.

The last deliverable of C3, and the one that cannot be done in the handler:
starlette parses and spools the whole multipart body before the handler is
entered, so by the time any of our code runs the bytes are already on disk. No
reordering inside /convert fixes that, which is why this belongs in middleware.
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

import pytest
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

import main


def test_starlette_has_no_total_body_limit():
    """max_part_size bounds ONE part, not the request.

    So a body of many parts, or one streamed without a Content-Length, has no
    ceiling from the framework.
    """
    assert MultiPartParser.spool_max_size == 1024 * 1024
    assert getattr(MultiPartParser, "max_file_size", None) is None


def test_the_app_installs_a_body_limit():
    names = [m.cls.__name__ for m in main.app.user_middleware if hasattr(m, "cls")]
    assert "BodySizeLimitMiddleware" in names, names


def test_a_declared_oversized_body_is_refused_before_it_is_read(monkeypatch):
    """The cheap check: Content-Length over the limit, turned away up front."""
    from bodylimit import BodySizeLimitMiddleware

    app = BodySizeLimitMiddleware(main.app, max_bytes=1024)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/convert",
        data={"biochef_workflow": '{"nodes": [], "edges": []}'},
        files=[("files", ("input-1-out", b"x" * 4096, "application/octet-stream"))],
    )

    assert response.status_code == 413, response.text
    assert "exceeds the limit" in response.text


def test_a_body_with_no_declared_length_is_also_refused():
    """The one that matters.

    Content-Length is trivially omitted -- chunked encoding and HTTP/2 both
    allow a body without it -- so a limit that only reads the header is a speed
    bump. This counts what actually arrives.

    Driven against a stub that drains the body, rather than against /convert.
    The real endpoint rejects on content-type before reading anything, so it
    would return 422 without the limit ever being reached -- a green test that
    proved nothing about the middleware.
    """
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    drained = {"bytes": 0}

    async def stub(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            drained["bytes"] += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BodySizeLimitMiddleware(stub, max_bytes=1024)
    chunks = [{"type": "http.request", "body": b"y" * 512, "more_body": True},
              {"type": "http.request", "body": b"y" * 512, "more_body": True},
              {"type": "http.request", "body": b"y" * 512, "more_body": False}]
    sent = []

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x",
             "headers": [], "query_string": b""}          # no content-length

    asyncio.new_event_loop().run_until_complete(app(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413, start["status"]
    assert drained["bytes"] <= 1024, \
        f"the application was fed {drained['bytes']} bytes past a 1024 limit"


def test_a_stream_within_the_limit_reaches_the_application():
    """The other half: the guard must not truncate a legitimate body."""
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    drained = {"bytes": 0}

    async def stub(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            drained["bytes"] += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BodySizeLimitMiddleware(stub, max_bytes=4096)
    chunks = [{"type": "http.request", "body": b"y" * 512, "more_body": True},
              {"type": "http.request", "body": b"y" * 512, "more_body": False}]
    sent = []

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.new_event_loop().run_until_complete(
        app({"type": "http", "method": "POST", "path": "/x",
             "headers": [], "query_string": b""}, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert drained["bytes"] == 1024


def _status_for(total_bytes, limit):
    """Drive the middleware with one body of `total_bytes` against `limit`."""
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    async def stub(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    def run(headers, chunks):
        app = BodySizeLimitMiddleware(stub, max_bytes=limit)
        pending = list(chunks)
        sent = []

        async def receive():
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.new_event_loop().run_until_complete(
            app({"type": "http", "method": "POST", "path": "/x",
                 "headers": headers, "query_string": b""}, receive, send))
        return next(m for m in sent if m["type"] == "http.response.start")["status"]

    body = [{"type": "http.request", "body": b"z" * total_bytes, "more_body": False}]
    declared = run([(b"content-length", str(total_bytes).encode())], body)
    streamed = run([], body)
    return declared, streamed


def test_the_limit_is_inclusive_and_both_paths_agree():
    """Pin the boundary. Without this, `>` -> `>=` survives the whole suite.

    Checked by mutation: flipping either comparison to `>=` was caught by no
    other test in this file. An off-by-one here is not academic -- it decides
    whether a body of exactly the configured size is served or refused, and if
    the two paths disagree the same request succeeds or fails depending on
    whether the client happened to send a Content-Length.
    """
    assert _status_for(1023, 1024) == (200, 200)
    assert _status_for(1024, 1024) == (200, 200), "at exactly the limit must pass"
    assert _status_for(1025, 1024) == (413, 413), "one byte over must be refused"


def test_a_body_within_the_limit_reaches_the_application_intact():
    """Replaces an assertion of `status_code != 413`, which proved nothing.

    That assertion was satisfied by any status at all, including one produced
    after the middleware had truncated the body -- it caught none of the eight
    mutations tried against this module. What matters is that the whole body
    arrives, so this counts the bytes the application actually received.
    """
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    seen = {"bytes": 0}

    async def stub(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            seen["bytes"] += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BodySizeLimitMiddleware(stub, max_bytes=8 * 1024 * 1024)
    pending = [{"type": "http.request", "body": b"x" * 4096, "more_body": True},
               {"type": "http.request", "body": b"x" * 4096, "more_body": False}]
    sent = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.new_event_loop().run_until_complete(
        app({"type": "http", "method": "POST", "path": "/x",
             "headers": [], "query_string": b""}, receive, send))

    assert next(m for m in sent
                if m["type"] == "http.response.start")["status"] == 200
    assert seen["bytes"] == 8192, f"the body was truncated to {seen['bytes']}"


def test_an_unusable_limit_is_refused_at_startup(monkeypatch):
    """A typo should name itself, not surface as an int() error."""
    from bodylimit import _limit_from_env

    for bad in ("512MB", "", "not a number"):
        with pytest.raises(ValueError, match="BIOCHEF_MAX_UPLOAD_BYTES"):
            _limit_from_env(bad)
    for bad in ("0", "-1"):
        with pytest.raises(ValueError, match="must be positive"):
            _limit_from_env(bad)

    assert _limit_from_env("  4096  ") == 4096


def test_the_default_limit_is_a_real_number():
    from bodylimit import MAX_UPLOAD_BYTES

    assert isinstance(MAX_UPLOAD_BYTES, int) and MAX_UPLOAD_BYTES > 0


