"""A ceiling on how many bytes a request may send.

The last piece of C3 (#11), and the one that has to live here rather than in the
handler: starlette parses and spools the whole multipart body before the handler
is entered, so by the time /convert runs the bytes are already on disk. Refusing
them there refuses nothing.

Two checks, because one is not enough.

`Content-Length` is the cheap one: a client that declares an oversized body is
turned away before a byte of it is read. It is also the one an attacker simply
omits -- HTTP/1.1 chunked encoding and HTTP/2 both allow a body with no declared
length -- so on its own it is a speed bump rather than a limit.

Counting what actually arrives is the real one. The ASGI `receive` callable is
wrapped, and the running total is checked as each chunk comes in, so a body with
no declared length is cut off at the same ceiling as one that declares itself
honestly.
"""

import json
import os

from starlette.datastructures import Headers


def _limit_from_env(raw):
    """Read the limit, and refuse to start on a value that is not one.

    int() alone gives "invalid literal for int() with base 10: \'512MB\'", which
    names neither the variable at fault nor the units expected. A service that
    will not start is the right outcome -- guessing at 512MB would be worse --
    but the operator should be told what to fix.

    A non-positive limit is refused for the same reason. It is technically
    fail-closed, but it means every request is answered 413 with nothing saying
    why, which is a worse failure than not starting at all.
    """
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"BIOCHEF_MAX_UPLOAD_BYTES must be a whole number of bytes, "
            f"got {raw!r}. Suffixes like 'MB' are not accepted; "
            f"512 MiB is 536870912."
        ) from None
    if value <= 0:
        raise ValueError(
            f"BIOCHEF_MAX_UPLOAD_BYTES must be positive, got {value}. "
            f"A limit of {value} refuses every request."
        )
    return value


MAX_UPLOAD_BYTES = _limit_from_env(os.getenv("BIOCHEF_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
"""Default 512 MiB.

Chosen to be larger than any single input the catalogue's tools plausibly take
while still bounding the damage one request can do. It is configuration rather
than policy -- a deployment with a disk quota or a slower link should set it
lower, and one doing whole-genome work will need it higher. What matters is that
a limit exists at all; the number is the operator's.
"""


class BodySizeLimitMiddleware:
    """Refuse a request body over the limit, declared or not.

    Pure ASGI rather than BaseHTTPMiddleware, because BaseHTTPMiddleware reads
    the request to hand it on and would itself buffer the body this exists to
    avoid buffering.
    """

    def __init__(self, app, max_bytes: int = None):
        self.app = app
        self.max_bytes = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._too_large(send, int(declared))
                    return
            except ValueError:
                # A Content-Length that is not a number is a malformed request;
                # let the server decide, rather than guessing at its intent.
                pass

        received = 0
        refused = False

        async def counting_receive():
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Stop feeding the application. It sees the stream end here
                    # rather than a truncated body it might treat as complete.
                    refused = True
                    return {"type": "http.disconnect"}
            return message

        replaced = False

        async def guarded_send(message):
            # Once the body has been refused, the application's response is not
            # the truth about what happened, and the whole of it has to go --
            # status line, headers and body.
            #
            # Rewriting only the status was not enough, and produced something
            # worse than either half: the application sees its stream stop
            # early, FastAPI's blanket except around form parsing turns that
            # into "There was an error parsing the body", and the status alone
            # was swapped to 413. The client was told, accurately, that it had
            # sent too much, and in the same breath told the reason was a
            # malformed multipart body. Confirmed against real uvicorn on both
            # httptools and h11. The two paths also disagreed: a declared
            # Content-Length gave "exceeds the limit of N" and a streamed body
            # gave the parse error, for the same condition.
            #
            # The application's headers have to go with it. They describe a
            # response that is no longer being sent -- a Content-Length for a
            # body we are discarding, or a Content-Disposition offering the
            # client a download that does not exist.
            nonlocal replaced
            if not refused:
                await send(message)
                return
            if message["type"] == "http.response.start":
                await self._send_refusal(
                    send,
                    f"request body exceeds the limit of {self.max_bytes} bytes",
                )
                replaced = True
                return
            # Swallow whatever the application was going to say, including any
            # further streamed chunks. Our own response is already complete.
            if message["type"] == "http.response.body" and replaced:
                return
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _send_refusal(self, send, detail: str):
        """The one place a refusal is written, so both paths phrase it alike."""
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _too_large(self, send, declared: int):
        # Routed through the same writer as the streaming refusal, so the two
        # paths cannot drift apart in wording again. It also drops a
        # JSONResponse that was being called with None for its receive
        # callable and a scope containing only "type" -- it happens to work on
        # the installed starlette, which reads neither, but nothing about the
        # ASGI contract promises that.
        await self._send_refusal(
            send,
            f"request body of {declared} bytes exceeds the "
            f"limit of {self.max_bytes} bytes",
        )
