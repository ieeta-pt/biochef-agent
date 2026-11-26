"""Who is using the page (section 4 of the spec).

The agent has no accounts and no passwords: the TRE decides who may open the
page, and this only reads the answer. So the whole question is whether the answer
can be believed, and it is refused rather than guessed whenever it cannot. That
covers static files too: nothing is served to someone unknown. Websockets are
closed for everyone, since the page uses none and CSRF does not cover them.

The header is believed only from an address in BIOCHEF_UI_TRUSTED_PROXIES, and
that address has to be the one the socket reports. run-ui.sh turns off uvicorn's
proxy headers because otherwise uvicorn rewrites it from X-Forwarded-For.

With BIOCHEF_UI_SOCKET there is no address to check: the page listens on a unix
socket, the directory around it decides who may connect, and whoever did is the
proxy. Only a request that came in on that socket counts.
"""

import ipaddress

from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketClose

from ui.settings import DEV

# RFC 9110 requires this header on a 401. The scheme is not one a browser can
# answer, because the only way in is through the TRE.
CHALLENGE = {"WWW-Authenticate": "TRE-Proxy"}

# The name goes in the audit trail and in signed decisions, so it gets the same
# ceiling the store puts on a member's name (store/repo.py MAX_NAME_LENGTH).
# Not imported from there, because the page does not load the store yet.
MAX_USER_LENGTH = 128

# RFC 6455 "policy violation": the generic refusal.
WEBSOCKET_REFUSED = 1008


def _address(scope):
    """The peer's address, or None when it is not one (a unix socket, say)."""
    client = scope.get("client")
    if not client:
        return None
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return None
    # A dual stack listener reports an IPv4 peer as ::ffff:a.b.c.d, which no
    # IPv4 network contains and which is_loopback does not recognise.
    return getattr(address, "ipv4_mapped", None) or address


def _named_user(scope, name):
    """The single usable value of a header, or None."""
    wanted = name.lower().encode("latin-1")
    values = [value for key, value in scope["headers"] if key == wanted]
    if len(values) != 1:
        return None
    try:
        user = values[0].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    # The name goes in the audit trail and in signed decisions. isprintable also
    # refuses line breaks and other separators.
    if not user or len(user) > MAX_USER_LENGTH or not user.isprintable():
        return None
    return user


def _on_the_socket(scope, settings):
    # uvicorn reports no peer on a unix socket, and the socket's path as the server.
    return scope.get("client") is None and scope.get("server") == (settings.socket, None)


def who(scope, settings):
    """The user behind this request, or None when there is no believing it."""
    if settings.socket is not None:
        if settings.identity == DEV or not _on_the_socket(scope, settings):
            return None
        return _named_user(scope, settings.identity_header)
    address = _address(scope)
    if address is None:
        return None
    if settings.identity == DEV:
        # BIOCHEF_UI_HOST is checked at start, but a --host on the command line
        # gets past that, and this is the check that holds either way.
        return settings.dev_user if address.is_loopback else None
    if not any(address in network for network in settings.trusted_proxies):
        return None
    return _named_user(scope, settings.identity_header)


class IdentityMiddleware:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            # Closed before the app sees it. Otherwise a websocket route would
            # be served to anyone, and one under /ui/static/ crashes StaticFiles.
            return await WebSocketClose(code=WEBSOCKET_REFUSED)(scope, receive, send)
        if scope["type"] != "http":  # lifespan
            return await self.app(scope, receive, send)
        user = who(scope, self.settings)
        if user is None:
            refusal = PlainTextResponse("Open this page through the TRE.\n",
                                        status_code=401, headers=CHALLENGE)
            return await refusal(scope, receive, send)
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)
