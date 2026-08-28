"""Who may ask this service to run something (#10).

The same shape as runner.py, and for the same reason. An interface, providers
selected by name, and the policy that every provider shares written once. F3
(Passports) should be a third provider here rather than a rewrite of the second.

What a provider decides is narrow on purpose: given a request, either it names a
caller or it refuses. It does not decide what that caller may do -- there is no
authorisation model yet, and pretending otherwise by returning roles nobody
consults would be worse than the gap.
"""

import hmac
import json
import os
from typing import Optional

from fastapi import HTTPException, Request


class Unauthenticated(HTTPException):
    """401, with the challenge the specification requires.

    A 401 without WWW-Authenticate is not a well-formed refusal: RFC 9110 makes
    the header mandatory on 401, and it is what tells a client which scheme to
    try. 403 would be the wrong code -- it means "you are known and still may
    not", which is a statement this service is not yet in a position to make.
    """

    def __init__(self, detail: str, scheme: str = "Bearer"):
        super().__init__(status_code=401, detail=detail,
                         headers={"WWW-Authenticate": scheme})


AUTH_TOKEN = os.getenv("BIOCHEF_AUTH_TOKEN", "")
"""The shared secret, when BIOCHEF_AUTH=bearer.

Read from the environment rather than a file or an argument, so it does not end
up in a process listing or in shell history.
"""


class AuthProvider:
    """Decides whether a request may proceed, and on whose behalf."""

    name = "provider"

    def authenticate(self, request: Request) -> Optional[str]:
        """Return an identity for the caller, or raise Unauthenticated.

        The return value is deliberately just a name. Nothing consults it yet;
        it exists so that a provider which knows more about the caller -- a
        Passport carries claims -- has somewhere to put it without changing the
        signature of everything that calls this.
        """
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class NoAuth(AuthProvider):
    """Current behaviour, kept as an explicit choice rather than an absence.

    Naming it matters. "No authentication" as a configured provider appears in
    the settings, can be logged, and can be seen to be wrong in a deployment
    review. The same state as an unconfigured service, but visible.
    """

    name = "none"

    def authenticate(self, request: Request) -> Optional[str]:
        return None


class BearerAuth(AuthProvider):
    """A single shared token, presented as `Authorization: Bearer <token>`.

    A shared secret is not identity, and this does not pretend to be: every
    holder is the same caller. It is the smallest thing that stops an open
    endpoint being open, and the step before F3.
    """

    name = "bearer"

    def __init__(self, token: str = None):
        token = AUTH_TOKEN if token is None else token
        # Fail at startup, not on the first request. A deployment that asked for
        # bearer and supplied no token would otherwise start, look configured,
        # and refuse every request -- or worse, if this compared against an empty
        # string, admit anyone who sent an empty one.
        if not token or not token.strip():
            raise ValueError(
                "BIOCHEF_AUTH=bearer needs BIOCHEF_AUTH_TOKEN set to a "
                "non-empty value. Refusing to start rather than run with a "
                "token nobody has to guess."
            )
        self._token = token

    def authenticate(self, request: Request) -> Optional[str]:
        header = request.headers.get("authorization")
        if not header:
            raise Unauthenticated("no credentials were presented")

        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise Unauthenticated("expected an Authorization: Bearer <token>")

        # compare_digest, not ==. String comparison returns as soon as it finds a
        # difference, so how long it takes leaks how much of the token was right,
        # and a token can be recovered a character at a time.
        if not hmac.compare_digest(presented, self._token):
            raise Unauthenticated("the token presented is not the one configured")

        return "bearer-token"


class AuthenticationMiddleware:
    """Refuse before the body is read, not after.

    A route dependency would be the obvious place, and it is the wrong one:
    starlette parses and spools the whole multipart payload before the endpoint
    is entered, so an anonymous caller would still have uploaded up to
    BIOCHEF_MAX_UPLOAD_BYTES before anything asked who they were. The same
    reason bodylimit.py is middleware.

    Headers are in the ASGI scope from the start, so this costs nothing and can
    answer before a single byte of body is accepted. It must therefore sit
    OUTSIDE the body limit -- added last, since starlette makes the last-added
    middleware outermost.
    """

    def __init__(self, app, provider: AuthProvider):
        self.app = app
        self.provider = provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            # Kept, not discarded. authenticate() is documented to return an
            # identity, and until now nothing held onto it -- so the audit trail
            # had no way to say who, and a provider that knows more about the
            # caller had nowhere to put it. None is a real answer here and is
            # recorded as one: the bearer provider has no identity to give.
            scope.setdefault("state", {})["caller"] = (
                self.provider.authenticate(Request(scope))
            )
            scope["state"]["authenticated_by"] = self.provider.name
        except HTTPException as refusal:
            body = json.dumps({"detail": refusal.detail}).encode()
            headers = [(b"content-type", b"application/json"),
                       (b"content-length", str(len(body)).encode())]
            for key, value in (refusal.headers or {}).items():
                headers.append((key.encode().lower(), value.encode()))
            await send({"type": "http.response.start",
                        "status": refusal.status_code, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


PROVIDERS = {
    NoAuth.name: NoAuth,
    BearerAuth.name: BearerAuth,
}


def get_auth(name: str) -> AuthProvider:
    """Resolve a provider by name, refusing to start on one that is not there.

    Deliberately the same shape as runner.get_runner. A name that is not a
    provider must stop the process: silently falling back would mean a typo in
    BIOCHEF_AUTH turns an authenticated deployment into an open one, which is
    the single worst way for this setting to fail.
    """
    try:
        provider = PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"BIOCHEF_AUTH={name!r} is not an authentication provider. "
            f"Available: {', '.join(sorted(PROVIDERS))}."
        ) from None
    return provider()
