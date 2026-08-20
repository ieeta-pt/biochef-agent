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

import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

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

        async def guarded_send(message):
            # Once the body has been refused, the application's own response is
            # not the truth about what happened. Replace its status so the
            # client is told why, rather than seeing whatever the handler made
            # of a stream that stopped early.
            if refused and message["type"] == "http.response.start":
                message = dict(message, status=413)
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _too_large(self, send, declared: int):
        response = JSONResponse(
            status_code=413,
            content={"detail": f"request body of {declared} bytes exceeds the "
                               f"limit of {self.max_bytes}"},
        )
        await response({"type": "http"}, None, send)
