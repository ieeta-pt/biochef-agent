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


def test_a_declared_oversized_body_is_refused_before_it_is_read():
    """The cheap check -- and "before it is read" is the property, not the status.

    Asserting only the status made this test blind: with the Content-Length
    check deleted outright, the streaming counter refuses the same request a
    moment later and the status is 413 either way. The mutation survived the
    whole suite. What actually distinguishes the two is whether the
    application was handed anything at all, so that is what is counted here.

    It matters because the two are not equivalent. The streaming path only
    refuses after max_bytes have been read and passed on; this path refuses
    before the first chunk is asked for.
    """
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    seen = {"bytes": 0, "receive_calls": 0}

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

    app = BodySizeLimitMiddleware(stub, max_bytes=1024)
    sent = []

    async def receive():
        seen["receive_calls"] += 1
        return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.new_event_loop().run_until_complete(
        app({"type": "http", "method": "POST", "path": "/x",
             "headers": [(b"content-length", b"4096")], "query_string": b""},
            receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert seen["receive_calls"] == 0, "the body was read despite being refused"
    assert seen["bytes"] == 0, f"{seen['bytes']} bytes reached the application"

    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    # The declared path knows the size, and says so; the streaming path cannot.
    assert b"request body of 4096 bytes exceeds" in body, body


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




# --------------------------------------------------------------------------
# what the refusal actually SAYS, which is not the same question as its status


_BOUNDARY = "----biochefuploadlimit"


def _multipart(payload_size):
    """A genuine /convert multipart body, padded to an exact size."""
    head = (f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="biochef_workflow"\r\n\r\n'
            '{"nodes": [], "edges": []}\r\n'
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="files"; '
            'filename="input-1-out"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{_BOUNDARY}--\r\n".encode()
    yield head
    sent = 0
    while sent < payload_size:
        n = min(4096, payload_size - sent)
        yield b"x" * n
        sent += n
    yield tail


def test_a_refusal_says_why_on_both_paths():
    """The status is not the message, and this is what the status alone hid.

    Rewriting only the status of the application's response left FastAPI's own
    body in place. On the streamed path the client was told 413 -- correctly --
    with the detail "There was an error parsing the body", which is a different
    failure entirely and says nothing about a size limit. The declared path
    meanwhile said "exceeds the limit of N", so the two disagreed about the
    reason for the same refusal.

    Confirmed against real uvicorn on both httptools and h11 before this test
    existed. Nothing here caught it, because every existing assertion stopped
    at the status code.
    """
    from bodylimit import BodySizeLimitMiddleware

    app = BodySizeLimitMiddleware(main.app, max_bytes=4096)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}

    # bytes -> httpx sets a Content-Length; a generator -> it does not.
    declared = client.post("/convert", headers=headers,
                           content=b"".join(_multipart(40000)))
    streamed = client.post("/convert", headers=headers,
                           content=_multipart(40000))

    for response, path in ((declared, "declared"), (streamed, "streamed")):
        assert response.status_code == 413, f"{path}: {response.status_code}"
        assert "exceeds the limit" in response.text, f"{path}: {response.text}"
        assert "parsing the body" not in response.text, (
            f"{path} is relaying the application's error instead of its own: "
            f"{response.text}"
        )
        assert response.headers["content-type"] == "application/json"


def test_the_application_headers_do_not_survive_a_refusal():
    """A 413 must not carry headers describing a response nobody sent.

    The application's Content-Disposition would offer the client a download
    that does not exist, and its Content-Length would describe a body that has
    been discarded.
    """
    import asyncio
    from bodylimit import BodySizeLimitMiddleware

    async def stub(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/octet-stream"),
            (b"content-disposition", b'attachment; filename="results.tar"'),
            (b"content-length", b"9"),
        ]})
        await send({"type": "http.response.body", "body": b"sensitive"})

    app = BodySizeLimitMiddleware(stub, max_bytes=1024)
    pending = [{"type": "http.request", "body": b"y" * 4096, "more_body": False}]
    sent = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.new_event_loop().run_until_complete(
        app({"type": "http", "method": "POST", "path": "/x",
             "headers": [], "query_string": b""}, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 413
    assert "content-disposition" not in headers, headers
    assert headers["content-type"] == "application/json"

    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    assert b"sensitive" not in body, body
    assert int(headers["content-length"]) == len(body), (
        f"declared {headers['content-length']} but sent {len(body)}")
