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
USERINFO = "https://broker.test/oidc/userinfo"


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

def userinfo_returning(*visas):
    """Stands in for the broker's UserInfo endpoint.

    This is where a passport actually comes from. Embedding visas in the access
    token, which every test here used to do, tested a shape the AAI profile
    forbids: "access tokens MUST NOT contain GA4GH Claims directly".
    """
    def fetch(url, access_token):
        assert access_token, "UserInfo must be called with the access token"
        return {passports.PASSPORT_CLAIM: list(visas)}
    return fetch


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
                         required_value="https://datasets.test/1",
                         userinfo_url=USERINFO, userinfo_fetch=userinfo_returning(controller.sign(visa_claims())))
    token = broker.sign(passport_claims())
    assert provider.authenticate(_Request(f"Bearer {token}")) == f"{BROKER}#user-1"


def test_requiring_a_visa_refuses_a_caller_without_it(broker, controller):
    provider = _provider(broker, controller,
                         visa_issuers=CONTROLLER,
                         required_visa="ControlledAccessGrants",
                         required_value="https://datasets.test/1",
                         userinfo_url=USERINFO, userinfo_fetch=userinfo_returning())
    token = broker.sign(passport_claims())  # a valid token, no visas at UserInfo
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
        userinfo_url=USERINFO, userinfo_fetch=userinfo_returning(
            attacker.sign(visa_claims(issuer="https://attacker.test"))),
    )
    token = broker.sign(passport_claims())
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


def test_auth_imports_without_the_passport_dependencies():
    """A deployment using none or bearer must not need PyJWT or cryptography.

    This is pinned because it is exactly the property a tidy-up removes without
    noticing. One did: moving `import passports` to the top of auth.py looked
    like cleaning up a function-level import, and quietly made every deployment
    fail to start without a dependency it never uses.

    Run in a subprocess with those modules blocked, because auth is already
    imported in this process and re-importing it would find the cached module.
    """
    import subprocess

    program = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def blocked(name, *a, **k):\n"
        "    if name.split('.')[0] in ('jwt', 'cryptography'):\n"
        "        raise ImportError(name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = blocked\n"
        "sys.path.insert(0, %r)\n"
        "import auth\n"
        "assert auth.get_auth('none')\n"
        "assert 'passport' in auth.PROVIDERS\n"
        "print('ok')\n" % str(REPO_ROOT)
    )
    done = subprocess.run([sys.executable, "-c", program],
                          capture_output=True, text=True)
    assert done.returncode == 0, (
        f"auth.py cannot be imported without the passport dependencies:\n"
        f"{done.stderr[-800:]}"
    )
    assert "ok" in done.stdout


def test_an_unreachable_issuer_does_not_stop_the_service_starting(monkeypatch):
    """The same outage, answered the same way whichever side of startup it falls.

    Building the key set in the constructor meant asking the issuer for its
    discovery document at boot, so a provider that was briefly unreachable
    stopped this service from starting and an orchestrator then restart-looped
    it. That outage during a request was already a 401, and there is no reason
    for the answer to depend on when the network happened to fail.
    """
    def unreachable(url):
        raise OSError("identity provider unreachable")

    monkeypatch.setattr(passports, "_fetch_json", unreachable)
    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE)
    assert provider.name == "passport"


def test_an_unreachable_issuer_is_a_401_on_the_first_request(broker, monkeypatch):
    def unreachable(url):
        raise OSError("identity provider unreachable")

    monkeypatch.setattr(passports, "_fetch_json", unreachable)
    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE)
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {broker.sign(passport_claims())}"))


def test_misconfiguration_is_still_fatal_at_startup():
    """Laziness applies to the network, not to configuration.

    A missing issuer, or a visa requirement with no trusted issuers, is a
    mistake nobody should discover from a 401 at three in the morning.
    """
    with pytest.raises(ValueError):
        auth.PassportAuth(issuer="", audience=AUDIENCE)
    with pytest.raises(ValueError):
        auth.PassportAuth(issuer=BROKER, audience="")
    with pytest.raises(ValueError):
        auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                          required_visa="ControlledAccessGrants", visa_issuers="")


def test_a_bug_in_this_file_is_not_disguised_as_a_refusal(broker):
    """`except Exception` would turn a TypeError of ours into a routine 401."""
    class _Broken:
        def key_for(self, kid):
            raise AttributeError("a bug, not a failed credential")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=lambda url, issuer: _Broken())
    with pytest.raises(AttributeError):
        provider.authenticate(_Request(f"Bearer {broker.sign(passport_claims())}"))


def test_an_outage_is_not_amplified_by_however_much_traffic_we_have(broker):
    """KeySet rate-limits its own refetches, but that guard lives inside a
    KeySet, and there is none yet when discovery is what failed. Without a limit
    here, a provider outage turned every arriving request into an outbound
    request at the provider."""
    attempts = []

    def failing(url, issuer):
        attempts.append(1)
        raise OSError("unreachable")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=failing)
    for _ in range(25):
        with pytest.raises(auth.Unauthenticated):
            provider.authenticate(_Request(f"Bearer {broker.sign(passport_claims())}"))
    assert len(attempts) == 1, f"{len(attempts)} discovery attempts for 25 requests"


def test_a_slow_discovery_does_not_serialise_every_other_caller(broker):
    """A real fetch has a ten second timeout. Holding the lock across it stalled
    every other caller's authentication behind one hanging call."""
    import threading as _threading

    def slow(url, issuer):
        time.sleep(0.3)
        raise OSError("slow, then unreachable")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=slow)
    token = f"Bearer {broker.sign(passport_claims())}"

    def hit():
        try:
            provider.authenticate(_Request(token))
        except auth.Unauthenticated:
            pass

    started = time.monotonic()
    threads = [_threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    assert elapsed < 0.9, (
        f"4 concurrent requests took {elapsed:.1f}s; serialised behind the lock"
    )


def test_the_keyset_is_built_once_when_the_issuer_answers(broker):
    built = []

    def counting(url, issuer):
        built.append(1)
        return keyset_for(broker)

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=counting)
    token = f"Bearer {broker.sign(passport_claims())}"
    for _ in range(5):
        assert provider.authenticate(_Request(token)) == f"{BROKER}#user-1"
    assert len(built) == 1


def test_a_healthy_cold_start_does_not_refuse_concurrent_callers(broker):
    """The regression this replaced.

    Timing the ATTEMPT rather than the FAILURE meant a construction still in
    flight looked like a fresh failure, so on a perfectly healthy cold start one
    request built the key set and every other concurrent one was told 401.
    Refusing a legitimate caller because of an internal race is a far worse
    answer than a redundant fetch.
    """
    import threading as _threading

    def slow_but_fine(url, issuer):
        time.sleep(0.4)
        return keyset_for(broker)

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=slow_but_fine)
    token = f"Bearer {broker.sign(passport_claims())}"
    outcomes = []

    def hit():
        try:
            outcomes.append(provider.authenticate(_Request(token)))
        except auth.Unauthenticated:
            outcomes.append(None)

    threads = [_threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    refused = [o for o in outcomes if o is None]
    assert not refused, (
        f"{len(refused)} of {len(outcomes)} legitimate concurrent callers were "
        f"refused during a successful cold start"
    )
    assert all(o == f"{BROKER}#user-1" for o in outcomes)


def test_a_concurrent_burst_against_a_dead_issuer_makes_one_call(broker):
    """Sequential traffic was already bounded to one attempt. Concurrent was
    bounded by nothing, which is the shape real traffic has: twenty-five
    simultaneous requests made twenty-five outbound calls at a provider that
    was already having a bad day."""
    import threading as _threading

    attempts = []
    counter = _threading.Lock()

    def failing(url, issuer):
        with counter:
            attempts.append(1)
        time.sleep(0.15)
        raise OSError("unreachable")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=failing)
    token = f"Bearer {broker.sign(passport_claims())}"

    def hit():
        try:
            provider.authenticate(_Request(token))
        except auth.Unauthenticated:
            pass

    threads = [_threading.Thread(target=hit) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(attempts) == 1, f"{len(attempts)} outbound calls for one outage"


def test_waiting_callers_do_not_each_fetch_on_a_healthy_cold_start(broker):
    """The same single-flight property, on the path that succeeds."""
    import threading as _threading

    built = []
    counter = _threading.Lock()

    def slow_but_fine(url, issuer):
        with counter:
            built.append(1)
        time.sleep(0.4)
        return keyset_for(broker)

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=slow_but_fine)
    token = f"Bearer {broker.sign(passport_claims())}"
    outcomes = []

    def hit():
        try:
            outcomes.append(provider.authenticate(_Request(token)))
        except auth.Unauthenticated:
            outcomes.append(None)

    threads = [_threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1, f"{len(built)} resolutions for one cold start"
    assert not [o for o in outcomes if o is None], "a legitimate caller was refused"


def test_a_resolver_torn_down_does_not_wedge_authentication_permanently(broker):
    """`except Exception` does not catch a thread being torn down.

    A SystemExit during a shutdown, a KeyboardInterrupt, or anything else
    deriving from BaseException left the in-flight marker set and the event
    unsignalled. From then on every request waited the full fifteen seconds and
    was refused, permanently, with no way back short of a restart. The cleanup
    is in a finally so it happens for anything that leaves the frame.
    """
    import threading as _threading

    def torn_down(url, issuer):
        raise BaseException("thread torn down")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=torn_down)
    token = f"Bearer {broker.sign(passport_claims())}"

    def call():
        try:
            provider.authenticate(_Request(token))
        except BaseException:
            pass

    first = _threading.Thread(target=call)
    first.start()
    first.join()

    assert not provider._resolving, "the in-flight marker was left set"

    started = time.monotonic()
    call()
    assert time.monotonic() - started < 1.0, (
        "a later caller waited for a resolver that had already died"
    )


def _passport_with_visa(broker, controller):
    """A valid access token. The visas come from UserInfo, not from here."""
    return broker.sign(passport_claims())


def test_an_unreachable_visa_issuer_is_a_refusal_and_not_a_crash(broker, controller):
    """The visa path was a second, simpler copy of the token path, so three
    fixes made on one side never reached the other. This is the one that
    mattered most: an unreachable data controller came out as a 500."""
    def factory(url, issuer):
        if issuer == BROKER:
            return keyset_for(broker)
        raise OSError("visa issuer unreachable")

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE, visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants", keyset_factory=factory,
        userinfo_url=USERINFO, userinfo_fetch=userinfo_returning(controller.sign(visa_claims())))
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(
            _Request(f"Bearer {_passport_with_visa(broker, controller)}"))


def test_a_dead_visa_issuer_is_not_asked_once_per_request(broker, controller):
    """Same amplification guard as the token issuer, which the visa path did
    not have."""
    import threading as _threading

    calls = []
    counter = _threading.Lock()

    def factory(url, issuer):
        if issuer == BROKER:
            return keyset_for(broker)
        with counter:
            calls.append(1)
        time.sleep(0.1)
        raise OSError("unreachable")

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE, visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants", keyset_factory=factory,
        userinfo_url=USERINFO, userinfo_fetch=userinfo_returning(controller.sign(visa_claims())))
    token = f"Bearer {_passport_with_visa(broker, controller)}"

    def hit():
        try:
            provider.authenticate(_Request(token))
        except auth.Unauthenticated:
            pass

    threads = [_threading.Thread(target=hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, f"{len(calls)} outbound calls at a dead visa issuer"


def test_two_visa_issuers_resolve_in_parallel(broker):
    """The lock used to be held across the visa keyset construction, so two
    different data controllers could not be resolved at the same time -- and a
    slow one blocked the token issuer too, since they shared the lock."""
    import threading as _threading

    def slow(url, issuer):
        time.sleep(0.3)
        return keyset_for(broker)

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 visa_issuers="https://c1.test,https://c2.test",
                                 keyset_factory=slow)
    started = time.monotonic()
    threads = [_threading.Thread(target=provider._keyset_for, args=(f"https://c{i}.test",))
               for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"two issuers took {elapsed:.1f}s, serialised"


def test_a_passport_carrying_too_many_visas_is_refused(broker, controller):
    """Every visa from a trusted issuer costs a signature verification, and
    nothing else bounds how many a caller can send.

    Refused rather than truncated: examining the first N silently would deny a
    legitimate passport whose relevant visa sits past the cut, for a reason
    nobody could see. A wrong answer that looks right is worse than an error.
    """
    one = controller.sign(visa_claims())
    claims = passport_claims(**{passports.PASSPORT_CLAIM: [one] * (passports.MAX_VISAS + 1)})
    with pytest.raises(passports.PassportError) as caught:
        passports.raw_visas(claims)
    assert str(passports.MAX_VISAS) in str(caught.value)


def test_a_passport_at_the_limit_is_still_accepted(broker, controller):
    one = controller.sign(visa_claims())
    claims = passport_claims(**{passports.PASSPORT_CLAIM: [one] * passports.MAX_VISAS})
    assert len(passports.raw_visas(claims)) == passports.MAX_VISAS


def test_the_provider_answers_through_the_real_middleware(broker):
    """Everything else here builds a fake request whose headers are a plain
    dict. The middleware constructs a Starlette Request from an ASGI scope, and
    a provider that works against the stub and not against that would look
    entirely tested.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE,
        keyset_factory=lambda url, issuer: keyset_for(broker))

    app = FastAPI()

    @app.get("/who")
    async def who():
        return {"ok": True}

    app.add_middleware(auth.AuthenticationMiddleware, provider=provider)
    client = TestClient(app)
    token = broker.sign(passport_claims())

    refused = client.get("/who")
    assert refused.status_code == 401
    assert refused.headers.get("www-authenticate") == "Bearer"

    assert client.get("/who", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # A scheme is case-insensitive per RFC 9110, and a header name always is.
    assert client.get("/who", headers={"authorization": f"bearer {token}"}).status_code == 200
    assert client.get("/who", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_a_waiter_woken_by_a_failure_is_not_told_it_timed_out(broker):
    """Two different events, and reporting the wrong one costs somebody an
    afternoon looking for a slow provider that was in fact failing instantly."""
    import threading as _threading

    def slow_then_fail(url, issuer):
        time.sleep(0.3)
        raise OSError("provider down")

    provider = auth.PassportAuth(issuer=BROKER, audience=AUDIENCE,
                                 keyset_factory=slow_then_fail)
    messages = []

    def call():
        try:
            provider._resolve(BROKER, None)
        except OSError as exc:
            messages.append(str(exc))

    threads = [_threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(messages) == 3
    waiters = [m for m in messages if "provider down" not in m]
    assert waiters, "expected callers that waited on the first one"
    for message in waiters:
        assert "did not finish within" not in message, (
            f"a waiter woken by a failure was told it timed out: {message}"
        )
        assert "could not be resolved" in message


def test_the_keys_url_comes_from_the_issuers_own_discovery_document():
    """Read rather than assembled by convention.

    The conventional path is right for most providers and silently wrong for the
    rest, and silently wrong here means refusing every valid token.
    """
    seen = []

    def fetch(url):
        seen.append(url)
        return {"jwks_uri": "https://b.test/oidc/keys"}

    assert passports.jwks_url_for("https://b.test", fetch=fetch) == "https://b.test/oidc/keys"
    assert seen == ["https://b.test/.well-known/openid-configuration"]


def test_a_trailing_slash_on_the_issuer_does_not_double_up():
    seen = []

    def fetch(url):
        seen.append(url)
        return {"jwks_uri": "https://b.test/keys"}

    passports.jwks_url_for("https://b.test/", fetch=fetch)
    assert seen == ["https://b.test/.well-known/openid-configuration"]


def test_an_issuer_publishing_no_keys_url_is_refused():
    with pytest.raises(passports.PassportError) as caught:
        passports.jwks_url_for("https://b.test", fetch=lambda url: {})
    assert "jwks_uri" in str(caught.value)


def test_a_non_string_keys_url_is_refused():
    with pytest.raises(passports.PassportError):
        passports.jwks_url_for("https://b.test", fetch=lambda url: {"jwks_uri": 12})


def test_a_discovery_document_that_is_not_a_document_is_refused():
    """A proxy answering with nothing, or with something that is not an object,
    must not come out as an AttributeError somewhere later."""
    for answer in (None, [], "not a document"):
        with pytest.raises(passports.PassportError):
            passports.jwks_url_for("https://b.test", fetch=lambda url: answer)


def test_a_visa_embedded_in_the_access_token_is_not_consulted(broker, controller):
    """The defect this whole change fixes, pinned so it cannot come back.

    The AAI profile says "access tokens MUST NOT contain GA4GH Claims directly",
    so a conformant broker never puts visas there. Reading them off the token
    meant every caller holding a perfectly good passport was refused, while the
    tests passed because they minted tokens with the claim embedded, according
    to the same misunderstanding as the code.

    Here the token carries a visa that WOULD satisfy the requirement, and
    UserInfo carries nothing. The caller must be refused.
    """
    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE, visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants",
        keyset_factory=lambda url, issuer: keyset_for(
            broker if issuer == BROKER else controller),
        userinfo_url=USERINFO, userinfo_fetch=userinfo_returning())
    token = broker.sign(passport_claims(**{
        passports.PASSPORT_CLAIM: [controller.sign(visa_claims())]}))
    with pytest.raises(auth.Unauthenticated):
        provider.authenticate(_Request(f"Bearer {token}"))


def test_userinfo_is_called_with_the_access_token(broker, controller):
    """The token is the credential for fetching the passport, which is its
    entire role in this flow."""
    seen = {}

    def fetch(url, access_token):
        seen["url"] = url
        seen["token"] = access_token
        return {passports.PASSPORT_CLAIM: [controller.sign(visa_claims())]}

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE, visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants",
        keyset_factory=lambda url, issuer: keyset_for(
            broker if issuer == BROKER else controller),
        userinfo_url=USERINFO, userinfo_fetch=fetch)
    token = broker.sign(passport_claims())
    assert provider.authenticate(_Request(f"Bearer {token}")) == f"{BROKER}#user-1"
    assert seen["url"] == USERINFO
    assert seen["token"] == token


def test_the_passport_is_fetched_fresh_for_every_request(broker, controller):
    """A passport says what the holder may see now. A visa carries its own
    expiry precisely because that can stop being true between one request and
    the next, so caching it here would outlive the statement."""
    calls = []

    def fetch(url, access_token):
        calls.append(1)
        return {passports.PASSPORT_CLAIM: [controller.sign(visa_claims())]}

    provider = auth.PassportAuth(
        issuer=BROKER, audience=AUDIENCE, visa_issuers=CONTROLLER,
        required_visa="ControlledAccessGrants",
        keyset_factory=lambda url, issuer: keyset_for(
            broker if issuer == BROKER else controller),
        userinfo_url=USERINFO, userinfo_fetch=fetch)
    token = f"Bearer {broker.sign(passport_claims())}"
    for _ in range(3):
        provider.authenticate(_Request(token))
    assert len(calls) == 3


def test_a_userinfo_answer_that_is_not_an_object_is_refused():
    with pytest.raises(passports.PassportError):
        passports.fetch_passport(USERINFO, "tok", fetch=lambda u, t: "nope")


def test_the_userinfo_url_comes_from_discovery():
    document = {"userinfo_endpoint": "https://b.test/oidc/userinfo",
                "jwks_uri": "https://b.test/oidc/jwk"}
    assert passports.userinfo_url_for("https://b.test/", fetch=lambda u: document) \
        == "https://b.test/oidc/userinfo"
    with pytest.raises(passports.PassportError):
        passports.userinfo_url_for("https://b.test", fetch=lambda u: {})


LSAAI = "https://login.aai.lifescience-ri.eu/oidc/"
LSAAI_DISCOVERY = {
    "jwks_uri": "https://login.aai.lifescience-ri.eu/oidc/jwk",
    "userinfo_endpoint": "https://login.aai.lifescience-ri.eu/oidc/userinfo",
}


def test_the_real_lsaai_endpoints_are_accepted():
    """Taken from the live discovery document. The keys are at /oidc/jwk and
    not at any conventional path, which is why they are read from the issuer
    rather than assembled."""
    assert passports.jwks_url_for(LSAAI, fetch=lambda url: LSAAI_DISCOVERY) \
        == LSAAI_DISCOVERY["jwks_uri"]
    assert passports.userinfo_url_for(LSAAI, fetch=lambda url: LSAAI_DISCOVERY) \
        == LSAAI_DISCOVERY["userinfo_endpoint"]


def test_a_keys_url_on_another_host_is_refused():
    """The worse of the two, and a complete authentication bypass.

    Keys fetched from a host the issuer named validate tokens. Anything able to
    influence that document -- a misconfiguration, a stale copy, a compromised
    broker -- would have this service trusting an attacker's signing key.
    """
    document = {"jwks_uri": "https://attacker.example/keys"}
    with pytest.raises(passports.PassportError) as caught:
        passports.jwks_url_for(LSAAI, fetch=lambda url: document)
    assert "attacker.example" in str(caught.value)


def test_a_userinfo_url_on_another_host_is_refused():
    """This one sends every caller's access token to whoever is listening."""
    document = {"userinfo_endpoint": "https://attacker.example/collect"}
    with pytest.raises(passports.PassportError):
        passports.userinfo_url_for(LSAAI, fetch=lambda url: document)


def test_a_relative_or_hostless_endpoint_is_refused():
    for value in ("/oidc/jwk", "jwk", ""):
        with pytest.raises(passports.PassportError):
            passports.jwks_url_for(LSAAI, fetch=lambda url: {"jwks_uri": value})


def test_every_passport_setting_including_the_newest_is_documented():
    """The explicit list has to grow with the settings, or the guard covers the
    six that existed when it was written and none since."""
    import re

    source = (REPO_ROOT / "auth.py").read_text()
    names = sorted(set(re.findall(r'"(BIOCHEF_PASSPORT_[A-Z_]+)"', source)))
    assert len(names) >= 7, names
    for path in ("README.md", "example.env"):
        text = (REPO_ROOT / path).read_text()
        for name in names:
            assert name in text, f"{name} is read by the code but absent from {path}"


def test_a_redundant_default_port_is_not_a_different_host():
    """Comparing netloc directly refused a conformant broker over punctuation:
    https://b.test:443/oidc and https://b.test/oidc/jwk are the same host."""
    for issuer, url in (
        ("https://b.test:443/oidc", "https://b.test/oidc/jwk"),
        ("https://b.test/oidc", "https://b.test:443/oidc/jwk"),
        ("https://b.test:8443/oidc", "https://b.test:8443/oidc/jwk"),
    ):
        assert passports.jwks_url_for(issuer, fetch=lambda u: {"jwks_uri": url}) == url


def test_a_genuinely_different_port_is_still_a_different_host():
    with pytest.raises(passports.PassportError):
        passports.jwks_url_for(
            "https://b.test:8443/oidc",
            fetch=lambda url: {"jwks_uri": "https://b.test:9999/jwk"})


def test_an_issuer_that_is_not_an_https_url_refuses_to_start():
    """Configuration mistakes are startup mistakes, the same as a missing
    audience. An issuer whose discovery document can never be fetched would
    otherwise refuse every request for a reason that looks like the caller's
    fault, and a plain-http issuer is one whose keys can be replaced in transit.
    """
    for bad in ("not-a-url", "http://b.test/oidc", "b.test", "https:///oidc"):
        with pytest.raises(ValueError) as caught:
            auth.PassportAuth(issuer=bad, audience=AUDIENCE,
                              keyset_factory=lambda url, issuer: None)
        assert "ISSUER" in str(caught.value)


def test_the_real_lsaai_issuer_is_accepted():
    provider = auth.PassportAuth(issuer=LSAAI, audience=AUDIENCE,
                                 keyset_factory=lambda url, issuer: None)
    assert provider.name == "passport"
