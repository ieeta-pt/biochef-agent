"""A handle tying one of a hub's requests to what this agent did with it (#84).

A hub fanning work out to several agents gets nothing back it can correlate on.
`POST /runs` answers `503` at capacity and `401` on bad credentials, and both
carry a detail string and nothing else -- no `run_id`, because no run was
created. A hub that sent ten workflows and got three refusals cannot say which
three.

So: take the caller's id when it gives one, make one up when it does not, put it
on every response, and record it on the run.

The inbound value is caller-supplied and gets written into a response header,
which makes it untrusted input on a path that validates nothing today. The
failure to guard against is not header injection -- h11, which uvicorn speaks,
refuses to write an illegal value -- but something worse in practice: an
otherwise-fine response destroyed by `LocalProtocolError` because of a
cosmetic header. A caller could delete its own successful submissions.

An unacceptable value is therefore REPLACED rather than refused. Rejecting a
workflow submission over a header nobody needs would be a worse failure than
ignoring the header, and the response always carries the id that was actually
used, so a caller comparing it against what it sent can see its value was not
taken.

This is not tracing. W3C `traceparent` is the real standard for that and needs
span ids, sampling decisions and a story about what gets propagated onward; a
plain request id does not block it and can be carried alongside it later. Nor
is it logging: this service logs nothing at all today, so there is nothing for
an id to tag. Request logging that carries it is separate work.
"""

import re
import uuid

from starlette.datastructures import Headers, MutableHeaders

HEADER = "x-request-id"

MAX_LENGTH = 128
"""Long enough for a UUID with a prefix, short enough to repeat everywhere.

The value is echoed on every response and stored on a retained run record, so
its length is multiplied by MAX_RUNS. The server caps total header size, but
that cap is the server's to change and says nothing about what this service
agrees to keep.
"""

ACCEPTABLE = re.compile(r"\A[A-Za-z0-9._~:/+=@-]{1,%d}\Z" % MAX_LENGTH)
"""Characters that are unambiguously legal in an HTTP field value.

Deliberately narrower than what h11 would accept. The point is not to permit as
much as possible but to be obviously safe to echo: no CR, no LF, no NUL, no
control characters, no non-ASCII, nothing needing quoting. A test checks every
generated and accepted id against h11 itself rather than against this pattern,
because a pattern I wrote agreeing with an implementation I wrote proves
nothing about what a real server will send.
"""


def generate() -> str:
    """A fresh id. Same shape as a run_id, for one less thing to explain."""
    return uuid.uuid4().hex


def accept(offered):
    """The id to use, and whether it came from the caller.

    Returns (request_id, from_caller). `from_caller` is False both when nothing
    was offered and when what was offered could not be used -- the caller sees
    which by comparing the echoed id against what it sent.
    """
    if offered is None or not ACCEPTABLE.match(offered):
        return generate(), False
    return offered, True


class RequestIdMiddleware:
    """Give every request an id, and put it on the way out.

    Pure ASGI rather than BaseHTTPMiddleware, because this has to tag responses
    that BaseHTTPMiddleware never sees as responses -- the 401 the
    authentication layer writes directly, and the 413 the body size limit writes
    without ever calling the application.

    It must be the OUTERMOST layer, which means added LAST, or the refusals it
    exists to label are produced beneath it and leave untagged.

    One thing it does not reach: the 500 that starlette's own
    ServerErrorMiddleware writes sits outside every middleware the application
    adds, so an unhandled exception comes back without an id. Tagging that would
    mean owning the exception handler, which is a different change.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # getlist, not get: two values mean a proxy added its own alongside the
        # caller's, and picking silently would be arbitrary. The first wins,
        # which is the convention every header getter already follows and the
        # one a caller is most likely to be matching on -- recorded in a test
        # so it is a decision rather than an accident.
        offered = Headers(scope=scope).getlist(HEADER)
        request_id, _ = accept(offered[0] if offered else None)

        # Where a handler reads it from. scope["state"] is what request.state
        # is a view of, so a route can take it without this module being
        # imported there.
        scope.setdefault("state", {})["request_id"] = request_id

        async def tagged(message):
            if message["type"] == "http.response.start":
                # Assigned, not appended: if something downstream already set
                # one, two values on the way out would be exactly the ambiguity
                # this refuses to accept on the way in.
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        await self.app(scope, receive, tagged)
