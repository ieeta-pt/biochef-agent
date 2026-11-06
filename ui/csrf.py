"""Every state change carries a token a page of another site cannot read.

The token lives in an HttpOnly cookie and is repeated in the page, so a POST has
to prove it could read the page. htmx sends it as a header (static/app.js), and
plain forms in a hidden field. It is checked here rather than route by route, so
a route added later cannot forget it.
"""

import hmac
import re
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

HEADER_NAME = "x-csrf-token"
FORM_FIELD = "csrf_token"

STATIC_PREFIX = "/ui/static/"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# A form on this page holds a choice and a comment, so this is generous. The
# body has to be read to find the field, and that must not be unbounded for a
# request nobody has vouched for yet.
MAX_FORM_BYTES = 1024 * 1024

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def cookie_name(secure):
    # `__Host-` makes the browser refuse the cookie from a sibling host or
    # without Secure, which closes the way around a cookie held token. It needs
    # https, so dev goes without.
    return "__Host-biochef-csrf" if secure else "biochef-csrf"


def _cookie_token(headers, name):
    jar = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except Exception:  # http.cookies raises its own error on a malformed header
        return None
    morsel = jar.get(name)
    return morsel.value if morsel and TOKEN.fullmatch(morsel.value) else None


def _matches(sent, token):
    return bool(sent) and hmac.compare_digest(sent.encode(), token.encode())


async def _read_body(receive):
    """The whole body and its messages, or None when it is over the limit."""
    messages, body = [], b""
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return body, messages
        body += message.get("body", b"")
        if len(body) > MAX_FORM_BYTES:
            return None, messages
        if not message.get("more_body"):
            return body, messages


def _replaying(messages, receive):
    pending = list(messages)

    async def replay():
        return pending.pop(0) if pending else await receive()
    return replay


class CsrfMiddleware:
    def __init__(self, app, settings):
        self.app = app
        self.secure = settings.secure_cookies
        self.name = cookie_name(self.secure)

    def _set_cookie(self, headers, token):
        attributes = f"{self.name}={token}; Path=/; HttpOnly; SameSite=Strict"
        if self.secure:
            attributes += "; Secure"
        headers.append((b"set-cookie", attributes.encode()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(STATIC_PREFIX):
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        token = _cookie_token(headers, self.name)

        if scope["method"] not in SAFE_METHODS:
            refused = None
            if token is None:
                refused = 403
            elif HEADER_NAME in headers:
                refused = None if _matches(headers[HEADER_NAME], token) else 403
            else:
                refused, receive = await self._check_form(headers, token, receive)
            if refused:
                refusal = PlainTextResponse("Reload the page and try again.\n",
                                            status_code=refused)
                return await refusal(scope, receive, send)

        fresh = token is None
        token = token or secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = token

        async def with_cookie(message):
            if fresh and message["type"] == "http.response.start":
                self._set_cookie(message.setdefault("headers", []), token)
            await send(message)

        await self.app(scope, receive, with_cookie)

    async def _check_form(self, headers, token, receive):
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/x-www-form-urlencoded":
            return 403, receive
        body, messages = await _read_body(receive)
        receive = _replaying(messages, receive)
        if body is None:
            return 413, receive
        sent = parse_qs(body.decode("latin-1")).get(FORM_FIELD, [])
        return (None if len(sent) == 1 and _matches(sent[0], token) else 403), receive
