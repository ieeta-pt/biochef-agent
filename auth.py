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
import time
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


# How long to wait before asking an unreachable issuer where its keys are
# again. Mirrors passports.REFETCH_INTERVAL_SECONDS, and for the same reason:
# without it an outage at the provider is amplified by however much traffic
# this service happens to be receiving.
DISCOVERY_RETRY_SECONDS = 30

# How long a caller waits for another caller's resolution rather than starting
# one of its own. Comfortably longer than the ten second fetch timeout inside
# passports, so a slow but successful discovery is waited out instead of being
# turned into a refusal -- which is the mistake this replaced.
DISCOVERY_WAIT_SECONDS = 15

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
        self._jwks_url = _setting(jwks_url, "BIOCHEF_PASSPORT_JWKS_URL") or None
        # Resolved on first use, not here. Building it now means asking the
        # issuer for its discovery document at startup, so an identity provider
        # that is briefly unreachable stops this service from starting at all --
        # and an orchestrator then restart-loops it. The same outage DURING a
        # request is already a 401, and there is no reason for the answer to
        # depend on which side of startup the network happened to fail.
        #
        # Configuration is still checked above, and still fatal. A missing
        # issuer or a visa requirement with no trusted issuers is a mistake
        # nobody should discover from a 401 at three in the morning; an
        # unreachable host is not that kind of mistake.
        # All keyed by issuer, the token issuer included. Keeping the visa
        # keysets in a separate, simpler structure is what let three fixes to
        # the token path never reach the visa path.
        self._keysets = {}
        self._failures = {}
        self._resolving = {}
        self._lock = threading.Lock()

    def _token_keys(self):
        return self._resolve(self._issuer, self._jwks_url)

    def _keyset_for(self, issuer):
        return self._resolve(issuer, None)

    def _resolve(self, issuer, jwks_url):
        """One issuer's key set, resolved once however many callers want it.

        The token issuer is just another issuer here, and that is the point. The
        visa path used to be a second, simpler copy of this logic, so every fix
        made on the token side had to be found again on the visa side, and never
        was: holding the lock across the network call, no limit on retrying a
        dead issuer, and a failure escaping as a 500 instead of a refusal all
        survived there after being fixed here.

        Three properties, each absent at some point and each costing something.

        The clock counts FAILURES, not attempts. Timing the attempt made a
        resolution still in flight look like a fresh failure, so a healthy cold
        start with concurrent traffic refused every caller but one.

        One resolution at a time per issuer, others waiting on an event.
        Letting each caller start its own bounded outage amplification by
        concurrency rather than by anything, and real traffic is concurrent.

        The network call is outside the lock and the cleanup is in a finally.
        Holding the lock across a ten second fetch stalled every other caller,
        and clearing the marker only on Exception meant a thread torn down
        mid-resolution wedged authentication until a restart.
        """
        with self._lock:
            keyset = self._keysets.get(issuer)
            if keyset is not None:
                return keyset
            if time.monotonic() - self._failures.get(issuer, 0.0) < DISCOVERY_RETRY_SECONDS:
                # OSError rather than a bespoke type: the callers already treat
                # it as "could not establish the key", which is what it is.
                raise OSError(
                    f"the key set for {issuer} could not be resolved, and the "
                    f"last attempt failed less than {DISCOVERY_RETRY_SECONDS}s ago"
                )
            waiting = self._resolving.get(issuer)
            if waiting is None:
                mine = threading.Event()
                self._resolving[issuer] = mine

        if waiting is not None:
            # Waiting on the event, not holding the lock, and fetching nothing.
            waiting.wait(timeout=DISCOVERY_WAIT_SECONDS)
            with self._lock:
                keyset = self._keysets.get(issuer)
            if keyset is not None:
                return keyset
            raise OSError(
                f"another caller is resolving the key set for {issuer} and it "
                f"did not finish within {DISCOVERY_WAIT_SECONDS}s"
            )

        try:
            keyset = self._factory(jwks_url, issuer)
        except Exception:
            with self._lock:
                self._failures[issuer] = time.monotonic()
            raise
        else:
            with self._lock:
                self._keysets[issuer] = keyset
            return keyset
        finally:
            with self._lock:
                self._resolving.pop(issuer, None)
            mine.set()

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
            claims = passports.verify(presented, self._token_keys(),
                                      issuer=self._issuer,
                                      audience=self._audience)
        except (passports.PassportError, OSError, ValueError):
            # PassportError covers the token itself. OSError and ValueError
            # cover resolving where the issuer keeps its keys, which is now done
            # on first use and can fail for every reason a network call can.
            # Both end the same way, because "we could not establish the key"
            # and "the key says no" are both "not authenticated" to a caller.
            #
            # Deliberately not `except Exception`. A TypeError or an
            # AttributeError in this file is a bug of ours, and turning it into
            # a 401 would hide it behind an answer that looks routine.
            #
            # The reason is not echoed back either. Which of signature, issuer,
            # audience or expiry failed is a fact about our configuration, and
            # telling an unauthenticated caller is telling them how to aim.
            raise Unauthenticated("the passport presented was not accepted")

        subject = claims.get("sub")
        if not subject:
            raise Unauthenticated("the passport names no subject")

        if self._required_visa:
            # Wrapped for the same reason the token verification is. Resolving a
            # visa issuer's keys reaches the network, and until an audit looked,
            # an unreachable data controller came out of here as a 500.
            try:
                accepted, _ = passports.verify_visas(
                    passports.raw_visas(claims),
                    trusted_issuers=self._visa_issuers,
                    keyset_for=self._keyset_for,
                )
            except (passports.PassportError, OSError, ValueError):
                raise Unauthenticated("the passport presented was not accepted")
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
