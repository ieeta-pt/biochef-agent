"""The headers of section 9, on every response of the UI.

Outermost of all the middleware, so that a 401, a 403 and a 404 carry them too,
and a crash in a route is answered here for the same reason: Starlette's own 500
is sent from outside every middleware, where these headers would be missing.
"""

from starlette.responses import PlainTextResponse

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'")

STATIC_PREFIX = "/ui/static/"


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False
        cacheable = scope["path"].startswith(STATIC_PREFIX)

        async def with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = message.setdefault("headers", [])
                headers.extend([
                    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ])
                if not cacheable:
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        except Exception:
            if not started:
                failure = PlainTextResponse("Something failed on this node.\n",
                                            status_code=500)
                await failure(scope, receive, with_headers)
            raise
