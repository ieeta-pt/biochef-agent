"""GA4GH Passports and visas (#22, F3).

Everything here runs against keys generated in the test, served by a fake JWKS.
Nothing reaches the network, and nothing needs an identity provider, which is
why this was the item that could be built and checked properly in one go.

The tests are mostly attacks, because that is what this code is for. A passport
verifier that accepts a valid token is easy; one that refuses a token signed
with the wrong algorithm, or a visa the caller minted for themselves, is the
whole feature.
"""

import json
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras_mod = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import auth
import passports

BROKER = "https://broker.test"
CONTROLLER = "https://controller.test"
AUDIENCE = "biochef-agent"


class Signer:
    """One issuer's keypair, and a JWKS that publishes it."""

    def __init__(self, kid):
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.public_pem = self._key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def jwks(self):
        numbers = self._key.public_key().public_numbers()

        def b64(value):
            import base64
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig",
                          "alg": "RS256", "n": b64(numbers.n), "e": b64(numbers.e)}]}

    def sign(self, claims, alg="RS256", kid=None):
        return jwt.encode(claims, self.private_pem, algorithm=alg,
                          headers={"kid": kid or self.kid})


@pytest.fixture
def broker():
    return Signer("broker-key-1")


@pytest.fixture
def controller():
    return Signer("controller-key-1")


def keyset_for(signer, **kwargs):
    return passports.KeySet("https://keys.test/jwks",
                            fetch=lambda url: signer.jwks(), **kwargs)


def passport_claims(**overrides):
    now = int(time.time())
    claims = {"iss": BROKER, "sub": "user-1", "aud": AUDIENCE,
              "iat": now, "exp": now + 300}
    claims.update(overrides)
    return claims


def visa_claims(issuer=CONTROLLER, type="ControlledAccessGrants",
                value="https://datasets.test/1", **overrides):
    now = int(time.time())
    claims = {"iss": issuer, "sub": "user-1", "iat": now, "exp": now + 300,
              passports.VISA_CLAIM: {"type": type, "value": value,
                                     "asserted": now, "source": issuer,
                                     "by": "dac"}}
    claims.update(overrides)
    return claims


# --- the token itself --------------------------------------------------------

def test_a_valid_passport_verifies(broker):
    claims = passports.verify(broker.sign(passport_claims()), keyset_for(broker),
                              issuer=BROKER, audience=AUDIENCE)
    assert claims["sub"] == "user-1"


def test_a_token_from_another_issuer_is_refused(broker):
    token = broker.sign(passport_claims(iss="https://elsewhere.test"))
    with pytest.raises(passports.PassportError):
        passports.verify(token, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_an_expired_token_is_refused(broker):
    now = int(time.time())
    token = broker.sign(passport_claims(exp=now - 3600, iat=now - 7200))
    with pytest.raises(passports.PassportError):
        passports.verify(token, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_a_token_for_another_audience_is_refused(broker):
    token = broker.sign(passport_claims(aud="some-other-service"))
    with pytest.raises(passports.PassportError):
        passports.verify(token, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_a_token_without_an_expiry_is_refused(broker):
    claims = passport_claims()
    del claims["exp"]
    with pytest.raises(passports.PassportError):
        passports.verify(broker.sign(claims), keyset_for(broker),
                         issuer=BROKER, audience=AUDIENCE)


# --- algorithm confusion -----------------------------------------------------

def test_a_token_signed_with_the_public_key_as_an_hmac_secret_is_refused(broker):
    """The classic JWT attack.

    An RSA public key is public. If a verifier honours the token's own `alg`
    header, an attacker sets it to HS256 and signs with the key everyone can
    read. The defence is to never let the token choose, which is why algorithms
    is always passed explicitly.
    """
    # Built by hand. PyJWT refuses to ENCODE with a PEM public key as an HMAC
    # secret, which is a good guardrail and not one an attacker is bound by --
    # so using it here would have tested PyJWT's signing path instead of this
    # service's verifying one.
    import base64, hashlib, hmac as hmac_mod

    def segment(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")

    header = segment({"alg": "HS256", "typ": "JWT", "kid": broker.kid})
    payload = segment(passport_claims())
    signing_input = header + b"." + payload
    signature = base64.urlsafe_b64encode(
        hmac_mod.new(broker.public_pem.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + signature).decode()

    # Sanity: the forgery is well-formed, so a failure below is a refusal and
    # not a parse error that would have happened to any garbage string.
    assert jwt.get_unverified_header(forged)["alg"] == "HS256"

    with pytest.raises(passports.PassportError):
        passports.verify(forged, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_an_unsigned_token_is_refused(broker):
    forged = jwt.encode(passport_claims(), key=None, algorithm="none",
                        headers={"kid": broker.kid})
    with pytest.raises(passports.PassportError):
        passports.verify(forged, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_the_permitted_algorithms_are_all_asymmetric():
    """An HMAC family here would mean the verification key is a signing key, so
    anyone who can verify could also mint."""
    assert not any(alg.startswith("HS") for alg in passports.ALGORITHMS)
    assert "none" not in passports.ALGORITHMS


# --- keys --------------------------------------------------------------------

def test_a_token_naming_an_unknown_key_is_refused(broker):
    token = broker.sign(passport_claims(), kid="not-a-real-key")
    with pytest.raises(passports.PassportError):
        passports.verify(token, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_a_token_naming_no_key_is_refused(broker):
    token = jwt.encode(passport_claims(), broker.private_pem, algorithm="RS256")
    with pytest.raises(passports.PassportError):
        passports.verify(token, keyset_for(broker), issuer=BROKER, audience=AUDIENCE)


def test_an_invented_kid_does_not_buy_an_outbound_fetch_every_time(broker):
    """Otherwise a stream of forged tokens turns this service into a load
    generator aimed at the identity provider."""
    fetches = []

    def counting_fetch(url):
        fetches.append(url)
        return broker.jwks()

    keys = passports.KeySet("https://keys.test/jwks", fetch=counting_fetch,
                            refetch_interval=300)
    for _ in range(20):
        with pytest.raises(passports.PassportError):
            keys.key_for("invented")
    assert len(fetches) == 1, f"{len(fetches)} fetches for 20 forged key ids"


def test_keys_are_not_fetched_over_plain_http():
    """A key that can be replaced in transit is not a key."""
    with pytest.raises(passports.PassportError):
        passports._fetch_json("http://keys.test/jwks")


def test_a_rotated_key_is_picked_up(broker):
    """Rotation and forgery look identical from here, so both are handled the
    same way -- but rotation still has to work."""
    rotated = Signer("broker-key-2")
    documents = [broker.jwks(), rotated.jwks()]
    keys = passports.KeySet("https://keys.test/jwks",
                            fetch=lambda url: documents.pop(0) if documents else rotated.jwks(),
                            cache_seconds=0, refetch_interval=0)
    assert keys.key_for(broker.kid) is not None
    assert keys.key_for(rotated.kid) is not None


# --- visas -------------------------------------------------------------------

def test_a_visa_from_a_trusted_issuer_is_accepted(controller):
    accepted, rejected = passports.verify_visas(
        [controller.sign(visa_claims())],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert len(accepted) == 1
    assert accepted[0]["type"] == "ControlledAccessGrants"
    assert not rejected


def test_a_visa_the_caller_minted_for_themselves_is_not_accepted():
    """The attack this whole design is arranged around.

    A valid passport from a broker you trust can carry a visa signed by anybody,
    including the person presenting it. Verifying the access token says nothing
    whatever about its visas.
    """
    attacker = Signer("attacker-key")
    forged = attacker.sign(visa_claims(issuer="https://attacker.test"))

    accepted, rejected = passports.verify_visas(
        [forged],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(attacker),
    )
    assert accepted == []
    assert rejected and rejected[0][0] == "untrusted-issuer"


def test_a_visa_claiming_a_trusted_issuer_but_signed_by_someone_else_is_refused(controller):
    """Naming a trusted issuer is not being one."""
    attacker = Signer("attacker-key")
    forged = attacker.sign(visa_claims(issuer=CONTROLLER))

    accepted, rejected = passports.verify_visas(
        [forged],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert accepted == []
    assert rejected and rejected[0][0] == "invalid"


def test_an_unrelated_visa_does_not_break_the_request(controller):
    """A passport legitimately carries visas about institutions this service
    knows nothing about. Refusing the whole request because of one would make a
    caller's unrelated affiliations break their access here."""
    other = Signer("other-key")
    accepted, rejected = passports.verify_visas(
        [other.sign(visa_claims(issuer="https://unrelated.test")),
         controller.sign(visa_claims())],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert len(accepted) == 1
    assert len(rejected) == 1


def test_an_expired_visa_is_not_accepted(controller):
    now = int(time.time())
    accepted, rejected = passports.verify_visas(
        [controller.sign(visa_claims(exp=now - 60, iat=now - 600))],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert accepted == []
    assert rejected[0][0] == "invalid"


def test_a_visa_value_is_matched_exactly(controller):
    """A prefix match would let a visa for `.../1-public` satisfy a requirement
    for `.../1` -- invisible in a log, obvious in an incident report."""
    accepted, _ = passports.verify_visas(
        [controller.sign(visa_claims(value="https://datasets.test/1-public"))],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert passports.satisfies(accepted, "ControlledAccessGrants",
                               "https://datasets.test/1-public")
    assert not passports.satisfies(accepted, "ControlledAccessGrants",
                                   "https://datasets.test/1")


def test_a_requirement_with_no_value_matches_any_value_of_that_type(controller):
    accepted, _ = passports.verify_visas(
        [controller.sign(visa_claims(value="https://datasets.test/9"))],
        trusted_issuers={CONTROLLER},
        keyset_for=lambda issuer: keyset_for(controller),
    )
    assert passports.satisfies(accepted, "ControlledAccessGrants")
    assert not passports.satisfies(accepted, "ResearcherStatus")


# --- the provider ------------------------------------------------------------

def _provider(broker, controller, **kwargs):
    return auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE,
        keyset_factory=lambda url, issuer: keyset_for(
            broker if issuer == BROKER else controller),
        **kwargs,
    )


class _Request:
    def __init__(self, header=None):
        self.headers = {"authorization": header} if header else {}


def test_the_provider_names_the_caller_with_the_issuer(broker, controller):
    provider = _provider(broker, controller)
    identity = provider.authenticate(_Request(f"Bearer {broker.sign(passport_claims())}"))
    assert identity == f"{BROKER}#user-1", (
        "two brokers can each have a subject '12345'; recording only the second "
        "half would merge two people into one caller"
    )


def test_the_provider_refuses_without_credentials(broker, controller):
    with pytest.raises(auth.Unauthenticated):
        _provider(broker, controller).authenticate(_Request())


def test_the_refusal_does_not_say_which_check_failed(broker, controller):
    """Which of signature, issuer, audience or expiry failed is a fact about our
    configuration, and telling an unauthenticated caller is telling them how to
    aim."""
    provider = _provider(broker, controller)
    bad = broker.sign(passport_claims(iss="https://elsewhere.test"))
    with pytest.raises(auth.Unauthenticated) as caught:
        provider.authenticate(_Request(f"Bearer {bad}"))
    detail = str(caught.value.detail)
    for leak in ("issuer", "audience", "expired", "signature"):
        assert leak not in detail.lower(), f"the refusal leaks {leak!r}"


def test_requiring_a_visa_admits_a_caller_who_has_it(broker, controller):
    provider = _provider(broker, controller,
                         visa_issuers=CONTROLLER,
                         required_visa="ControlledAccessGrants",
                         required_value="https://datasets.test/1")
    token = broker.sign(passport_claims(**{
        passports.PASSPORT_CLAIM: [controller.sign(visa_claims())]}))
    assert provider.authenticate(_Request(f"Bearer {token}")) == f"{BROKER}#user-1"


def test_requiring_a_visa_refuses_a_caller_without_it(broker, controller):
    provider = _provider(broker, controller,
                         visa_issuers=CONTROLLER,
                         required_visa="ControlledAccessGrants",
                         required_value="https://datasets.test/1")
    token = broker.sign(passport_claims())  # a valid passport, carrying nothing
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {token}"))


def test_a_self_minted_visa_does_not_get_the_caller_in(broker):
    """End to end: a genuine passport from a trusted broker, carrying a visa the
    caller signed themselves."""
    attacker = Signer("attacker-key")
    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE,
        visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants",
        keyset_factory=lambda url, issuer: keyset_for(
            broker if issuer == BROKER else attacker),
    )
    token = broker.sign(passport_claims(**{
        passports.PASSPORT_CLAIM: [attacker.sign(visa_claims(issuer="https://attacker.test"))]}))
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {token}"))


# --- refusing to start -------------------------------------------------------

def test_no_issuer_refuses_to_start(monkeypatch):
    monkeypatch.delenv("BIOCHEF_PASSPORT_ISSUER", raising=False)
    with pytest.raises(ValueError) as caught:
        auth.PassportAuth(keyset_factory=lambda url, issuer: None)
    assert "ISSUER" in str(caught.value)


def test_requiring_a_visa_without_naming_issuers_refuses_to_start(broker):
    """The configuration that looks strictest and is weakest.

    Every issuer on the internet becomes an authority, and the caller can mint
    the very visa being required.
    """
    with pytest.raises(ValueError) as caught:
        auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                          required_visa="ControlledAccessGrants",
                          visa_issuers="",
                          keyset_factory=lambda url, issuer: keyset_for(broker))
    assert "VISA_ISSUERS" in str(caught.value)


def test_passport_is_a_third_provider_and_not_a_rewrite_of_the_second():
    assert set(auth.PROVIDERS) == {"none", "bearer", "passport"}
    assert auth.PROVIDERS["bearer"] is auth.BearerAuth


def test_every_passport_setting_is_documented():
    """These escape the guard that covers every other setting.

    test_settings_are_documented scans for `getenv("NAME")` with a literal, and
    these are read through a helper that takes the name as an argument -- so the
    six variables configuring authentication are exactly the ones that scan does
    not cover. Asserted here instead, against both files.
    """
    names = [
        "BIOCHEF_PASSPORT_ISSUER", "BIOCHEF_PASSPORT_AUDIENCE",
        "BIOCHEF_PASSPORT_JWKS_URL", "BIOCHEF_PASSPORT_VISA_ISSUERS",
        "BIOCHEF_PASSPORT_REQUIRE_VISA", "BIOCHEF_PASSPORT_REQUIRE_VISA_VALUE",
    ]
    source = (REPO_ROOT / "auth.py").read_text()
    for name in names:
        assert name in source, f"{name} is documented but nothing reads it"
    for path in ("README.md", "example.env"):
        text = (REPO_ROOT / path).read_text()
        for name in names:
            assert name in text, f"{name} is read by the code but absent from {path}"


def test_no_audience_refuses_to_start(broker, monkeypatch):
    """Without it, a token the same issuer minted for ANOTHER service is
    accepted here -- the caller never intended us to see it, and whoever holds
    it can replay it against us."""
    monkeypatch.delenv("BIOCHEF_PASSPORT_AUDIENCE", raising=False)
    with pytest.raises(ValueError) as caught:
        auth.PassportAuth(issuer=BROKER,
                          keyset_factory=lambda url, issuer: keyset_for(broker))
    assert "AUDIENCE" in str(caught.value)
    assert "'any'" in str(caught.value), "the message must say how to opt out"


def test_the_opt_out_is_spelled_out_rather_than_reached_by_an_empty_box(broker):
    """`any` is a real deployment for issuers that mint audience-less tokens.
    It stays possible; it just has to be typed."""
    provider = auth.PassportAuth(
        issuer=BROKER, audience="any",
        keyset_factory=lambda url, issuer: keyset_for(broker))
    assert provider._audience is None
    token = broker.sign(passport_claims(aud="some-entirely-other-service"))
    assert provider.authenticate(_Request(f"Bearer {token}")) == f"{BROKER}#user-1"


def test_a_configured_audience_refuses_a_token_minted_for_someone_else(broker):
    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE,
        keyset_factory=lambda url, issuer: keyset_for(broker))
    token = broker.sign(passport_claims(aud="some-entirely-other-service"))
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {token}"))


def test_a_jwks_outage_is_a_refusal_and_not_a_crash(broker):
    """A transient network failure must not escape as a 500. It refuses either
    way, so this is about the shape of the answer rather than about safety."""
    class _Down:
        def key_for(self, kid):
            raise ConnectionResetError("connection reset by peer")

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE,
        keyset_factory=lambda url, issuer: _Down())
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {broker.sign(passport_claims())}"))
