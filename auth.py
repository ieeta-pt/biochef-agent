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
import threading
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


class PassportAuth(AuthProvider):
    """A GA4GH Passport, and the visas it carries, checked one at a time.

    The third provider auth.py's docstring anticipated, and it keeps to the same
    narrow job: it names a caller or it refuses. A required visa is a condition
    of being let in, not an authorisation model -- what a named caller may then
    do is still undecided, and still deliberately so.

    Configuration is deliberately explicit and fails at startup rather than on
    the first request, exactly as bearer does. A deployment that asked for
    passports and named no issuer would otherwise start, look configured, and
    either refuse everything or -- far worse -- accept tokens from anywhere.
    """

    name = "passport"

    def __init__(self, issuer=None, audience=None, jwks_url=None,
                 visa_issuers=None, required_visa=None, required_value=None,
                 keyset_factory=None):
        self._issuer = _setting(issuer, "BIOCHEF_PASSPORT_ISSUER")
        if not self._issuer:
            raise ValueError(
                "BIOCHEF_AUTH=passport needs BIOCHEF_PASSPORT_ISSUER set to the "
                "issuer whose tokens this service accepts. Refusing to start "
                "rather than accept a token from anywhere."
            )

        # Required, with a spelled-out way to opt out. Without an audience
        # check, a passport the same issuer minted for a DIFFERENT service is
        # accepted here -- the caller never intended this service to see it, and
        # whoever holds it can replay it against us. That is a real deployment
        # for issuers that mint audience-less tokens, so it stays possible; it
        # just has to be typed out rather than reached by leaving a box empty.
        audience_setting = _setting(audience, "BIOCHEF_PASSPORT_AUDIENCE")
        if not audience_setting:
            raise ValueError(
                "BIOCHEF_AUTH=passport needs BIOCHEF_PASSPORT_AUDIENCE set to "
                "the audience this service is named by, so a token minted for "
                "another service cannot be replayed here. Set it to 'any' if "
                "the issuer genuinely mints tokens without an audience, and "
                "understand that any token from that issuer will be accepted."
            )
        self._audience = None if audience_setting == "any" else audience_setting

        raw_issuers = _setting(visa_issuers, "BIOCHEF_PASSPORT_VISA_ISSUERS")
        self._visa_issuers = frozenset(
            entry.strip() for entry in (raw_issuers or "").split(",") if entry.strip()
        )

        self._required_visa = _setting(required_visa,
                                       "BIOCHEF_PASSPORT_REQUIRE_VISA") or None
        self._required_value = _setting(required_value,
                                        "BIOCHEF_PASSPORT_REQUIRE_VISA_VALUE") or None

        # Requiring a visa without saying whose visas count is the configuration
        # that looks strictest and is weakest: every issuer on the internet
        # becomes an authority, and a caller can mint their own.
        if self._required_visa and not self._visa_issuers:
            raise ValueError(
                "BIOCHEF_PASSPORT_REQUIRE_VISA is set but "
                "BIOCHEF_PASSPORT_VISA_ISSUERS is empty. A visa is only worth "
                "anything if its issuer is one you named in advance -- otherwise "
                "anybody can mint themselves the visa you are requiring."
            )

        self._factory = keyset_factory or _default_keyset_factory
        self._token_keyset = self._factory(
            _setting(jwks_url, "BIOCHEF_PASSPORT_JWKS_URL") or None, self._issuer
        )
        self._visa_keysets = {}
        self._lock = threading.Lock()

    def _keyset_for(self, issuer):
        with self._lock:
            if issuer not in self._visa_keysets:
                self._visa_keysets[issuer] = self._factory(None, issuer)
            return self._visa_keysets[issuer]

    def authenticate(self, request: Request) -> Optional[str]:
        # Imported here, and NOT because of a circular import -- passports does
        # not import this module. It is imported late so that this file can be
        # imported at all without PyJWT and cryptography present, because the
        # none and bearer providers do not need them and a deployment using
        # either should not fail to start over a dependency it never uses.
        #
        # An earlier commit moved this to the top as tidying, on the grounds
        # that a function-level import looks like it is working around
        # something. It was working around something; the comment saying so was
        # what was missing.
        import passports

        header = request.headers.get("authorization")
        if not header:
            raise Unauthenticated("no credentials were presented")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise Unauthenticated("expected an Authorization: Bearer <passport>")

        try:
            claims = passports.verify(presented, self._token_keyset,
                                      issuer=self._issuer,
                                      audience=self._audience)
        except passports.PassportError as refusal:
            # The reason is not echoed back. Which of signature, issuer,
            # audience or expiry failed is a fact about our configuration, and
            # telling an unauthenticated caller is telling them how to aim.
            raise Unauthenticated("the passport presented was not accepted")

        subject = claims.get("sub")
        if not subject:
            raise Unauthenticated("the passport names no subject")

        if self._required_visa:
            accepted, _ = passports.verify_visas(
                passports.raw_visas(claims),
                trusted_issuers=self._visa_issuers,
                keyset_for=self._keyset_for,
            )
            if not passports.satisfies(accepted, self._required_visa,
                                       self._required_value):
                raise Unauthenticated(
                    "the passport carries no visa this service requires")

        # The issuer travels with the subject. Two brokers can each have a
        # subject "12345", and an audit trail recording only the second half
        # would merge two people into one caller.
        return f"{self._issuer}#{subject}"


def _setting(value, name):
    if value is not None:
        return value
    return (os.getenv(name, "") or "").strip()


def _default_keyset_factory(jwks_url, issuer):
    import passports

    return passports.KeySet(jwks_url or passports.jwks_url_for(issuer))


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
            self.provider.authenticate(Request(scope))
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
    PassportAuth.name: PassportAuth,
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
