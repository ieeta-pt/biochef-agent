"""GA4GH Passports and visas: validating them, and refusing to guess (#22, F3).

auth.py says a provider's job is narrow -- given a request, either it names a
caller or it refuses -- and that returning roles nobody consults would be worse
than the gap. This keeps to that. A visa here is a condition of being let in at
all, not an authorisation model: if the deployment requires one and the caller
does not have it, the caller is not authenticated. What a named caller may then
do is still undecided, and still deliberately so.

## The mistake this is written around

A passport is an access token carrying a `ga4gh_passport_v1` claim, and that
claim is a list of **visas, each of which is itself a signed JWT** -- frequently
signed by somebody other than whoever issued the access token. That is the whole
point of the design: a broker authenticates you, and a data controller
independently asserts what you may see.

Which means verifying the access token tells you **nothing** about its visas. A
valid token from a broker you trust can carry a visa minted by anyone at all,
including the caller. Every visa is therefore verified on its own, against its
own issuer's keys, and only from issuers the deployment named in advance.

Without that allowlist the feature is worse than absent: anybody can stand up an
issuer, mint themselves a ControlledAccessGrants visa for any dataset, and be
let in by a system that believes it is checking credentials.

## The other mistake

`algorithms` is always passed explicitly. A JWT names its own algorithm in a
header the attacker controls, so a verifier that trusts that header can be told
`none`, or told to treat an RSA *public* key as an HMAC secret -- which is
public, which means anyone can sign. Nothing here ever lets a token choose.
"""

import json
import threading
import time
import urllib.request
from datetime import timezone

import jwt
from jwt import PyJWKSet

# Asymmetric only, and named rather than defaulted. Every algorithm here
# verifies with a key that is safe to publish; an HMAC family in this list would
# mean the verification key is also a signing key.
ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256",
              "PS384", "PS512")

VISA_CLAIM = "ga4gh_visa_v1"
PASSPORT_CLAIM = "ga4gh_passport_v1"

# Clock skew tolerated on exp/nbf/iat. Small: this is the amount by which an
# expired credential is still accepted, and a generous value here is a quiet
# extension of every token's lifetime.
LEEWAY_SECONDS = 30

# How long a fetched key set is reused, and how often an unknown `kid` may force
# a refetch. The second is the one that matters: without it, a stream of tokens
# carrying invented kids turns every request into an outbound fetch, and the
# identity provider absorbs a denial of service aimed at this service.
CACHE_SECONDS = 300
REFETCH_INTERVAL_SECONDS = 30


class PassportError(Exception):
    """A passport or visa could not be accepted."""


class KeySet:
    """Somebody's public keys, fetched once and reused.

    Thread-safe because the provider is called from whatever request thread
    happens to arrive, and two of them missing the cache at the same moment must
    not become two fetches and a torn dictionary.
    """

    def __init__(self, url, fetch=None, cache_seconds=CACHE_SECONDS,
                 refetch_interval=REFETCH_INTERVAL_SECONDS):
        self.url = url
        self._fetch = fetch or _fetch_json
        self._cache_seconds = cache_seconds
        self._refetch_interval = refetch_interval
        self._lock = threading.Lock()
        self._keys = None
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    def _load(self):
        document = self._fetch(self.url)
        try:
            key_set = PyJWKSet.from_dict(document)
        except Exception as exc:
            raise PassportError(f"{self.url} is not a usable JWKS: {exc}") from exc
        return {key.key_id: key for key in key_set.keys if key.key_id}

    def key_for(self, kid):
        """The key with this id, fetching if it is not already held.

        An unknown kid is a legitimate event -- issuers rotate -- and also
        exactly what a forged token looks like. Both are handled the same way and
        at most once every refetch interval, so rotation is picked up without a
        forged kid being a free outbound request.
        """
        if not kid:
            raise PassportError("the token names no key id, so no key can be chosen")

        now = time.monotonic()
        with self._lock:
            fresh = self._keys is not None and (now - self._fetched_at) < self._cache_seconds
            if fresh and kid in self._keys:
                return self._keys[kid]

            if not fresh or kid not in (self._keys or {}):
                if self._keys is not None and (now - self._last_attempt) < self._refetch_interval:
                    if kid in (self._keys or {}):
                        return self._keys[kid]
                    raise PassportError(
                        f"no key {kid!r} at {self.url}, and it was checked "
                        f"recently enough that this is not being asked again"
                    )
                self._last_attempt = now
                self._keys = self._load()
                self._fetched_at = time.monotonic()

            try:
                return self._keys[kid]
            except KeyError:
                raise PassportError(f"no key {kid!r} at {self.url}") from None


def _fetch_json(url):
    if not url.startswith("https://"):
        # Keys fetched over plain HTTP can be replaced in transit, and a key
        # that can be replaced is not a key.
        raise PassportError(f"refusing to fetch keys over an insecure URL: {url}")
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def jwks_url_for(issuer, fetch=None):
    """Where an issuer says its keys are.

    Read from the issuer's own discovery document rather than assembled by
    convention, because the conventional path is right for most providers and
    silently wrong for the rest -- and "silently wrong" here means refusing every
    valid token.
    """
    fetch = fetch or _fetch_json
    base = issuer.rstrip("/")
    document = fetch(f"{base}/.well-known/openid-configuration")
    url = (document or {}).get("jwks_uri")
    if not url:
        raise PassportError(f"{issuer} publishes no jwks_uri")
    return url


def verify(token, keyset, *, issuer, audience=None):
    """One JWT, checked against one key set. Returns its claims.

    `audience` may be None, which means the token is not required to name one.
    That is a real configuration for some deployments and a bad default for
    most, which is why auth.py requires it to be set deliberately.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise PassportError(f"not a usable token: {exc}") from exc

    key = keyset.key_for(header.get("kid"))
    options = {"require": ["exp", "iss"], "verify_aud": audience is not None}
    try:
        return jwt.decode(
            token,
            key.key,
            # Never header.get("alg"). The header is the attacker's to write.
            algorithms=list(ALGORITHMS),
            issuer=issuer,
            audience=audience,
            leeway=LEEWAY_SECONDS,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise PassportError(f"token rejected: {exc}") from exc


def raw_visas(claims):
    """The visa JWTs a passport carries, unverified.

    Named `raw` because that is what they are at this point: strings out of a
    token, signed by whoever, and worth exactly nothing until each has been
    verified on its own.
    """
    carried = claims.get(PASSPORT_CLAIM) or []
    if not isinstance(carried, list):
        raise PassportError(f"{PASSPORT_CLAIM} is not a list")
    return [entry for entry in carried if isinstance(entry, str)]


def verify_visas(tokens, *, trusted_issuers, keyset_for):
    """Every visa that verifies against an issuer this deployment trusts.

    A visa from an untrusted issuer is dropped rather than refused: a passport
    legitimately carries visas for datasets this service knows nothing about,
    and refusing the whole request because one of them came from elsewhere would
    make a caller's unrelated affiliations break their access here.

    A visa that fails to VERIFY against an issuer we do trust is also dropped,
    not fatal, for the same reason -- but the two are different events and the
    caller of this function is told which is which.
    """
    accepted, rejected = [], []
    for token in tokens:
        try:
            issuer = (jwt.decode(token, options={"verify_signature": False})
                      .get("iss"))
        except jwt.PyJWTError as exc:
            rejected.append(("unreadable", str(exc)))
            continue

        if issuer not in trusted_issuers:
            # Not an error. This is the ordinary case for a passport carrying
            # visas that concern other institutions entirely.
            rejected.append(("untrusted-issuer", issuer))
            continue

        try:
            claims = verify(token, keyset_for(issuer), issuer=issuer)
        except PassportError as exc:
            rejected.append(("invalid", f"{issuer}: {exc}"))
            continue

        visa = claims.get(VISA_CLAIM)
        if not isinstance(visa, dict) or not visa.get("type"):
            rejected.append(("malformed", issuer))
            continue
        accepted.append({**visa, "iss": issuer, "sub": claims.get("sub")})
    return accepted, rejected


def satisfies(visas, required_type, required_value=None):
    """Whether the accepted visas include the one the deployment demands.

    The value is compared exactly. A prefix or substring match would let a visa
    for `https://example.org/datasets/1-public` satisfy a requirement for
    `https://example.org/datasets/1`, which is the kind of near-miss that is
    invisible in a log and obvious in an incident report.
    """
    if not required_type:
        return True
    for visa in visas:
        if visa.get("type") != required_type:
            continue
        if required_value is None or visa.get("value") == required_value:
            return True
    return False
