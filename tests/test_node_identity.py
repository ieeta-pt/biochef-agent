"""The ID token checks of section 7.3, one test each, and the nonce of 7.3.4."""

import base64
import hashlib
import time

import jwt
import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

import json

import passports
from node import identity
from node.identity import IdentityError, Verifier
from store import db, repo
from tests.store_support import fresh_database

ISSUER = "https://login.test"
EDITOR = "biochef-editor"
LS_AAI_ID = "abc123@lifescience-ri.eu"

# The test vector of 7.3.4. Synthetic keys; x starts with a zero byte, so the
# vector also shows that leading zeros are kept.
SIGNING_KEY = {"kty": "EC", "crv": "P-256",
               "x": "AO1cfutKPq_Pj_qgqcxF6mw7pf0zvS0bOTf3G-GON1s",
               "y": "HjpzF_k9FmBSD7iiJq3QrtNRFM_uF0xVckpsACGAf38"}
RESULT_KEY = "age1vpw3jerf7764tnxv0jvwg9umcmstqf2zxwvezp4xpeetjxwsgsdsesfryj"
CANONICAL = ('{"result_key":"age1vpw3jerf7764tnxv0jvwg9umcmstqf2zxwvezp4xpeetjxwsgsdsesfryj",'
             '"signing_key":{"crv":"P-256","kty":"EC",'
             '"x":"AO1cfutKPq_Pj_qgqcxF6mw7pf0zvS0bOTf3G-GON1s",'
             '"y":"HjpzF_k9FmBSD7iiJq3QrtNRFM_uF0xVckpsACGAf38"},'
             '"v":"biochef.member-keys.v1"}')
NONCE = "ZU6yVFLLXiQ1WErqbRJRX9gXa47goE8iHMpLPnozw4A"


class Issuer:
    def __init__(self, kid="k1"):
        self.kid = kid
        self.key = ec.generate_private_key(ec.SECP256R1())

    def jwks(self):
        entry = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**entry, "kid": self.kid, "alg": "ES256", "use": "sig"}]}

    def token(self, **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": EDITOR, "sub": LS_AAI_ID, "exp": now + 300,
                  "iat": now, "name": "Ana Costa", "nonce": NONCE}
        claims.update(overrides)
        claims = {name: value for name, value in claims.items() if value is not None}
        return jwt.encode(claims, self.key, algorithm="ES256",
                          headers={"kid": self.kid})


LOGIN = Issuer()


def keyset(issuer=LOGIN):
    return passports.KeySet("https://login.test/jwks", fetch=lambda url: issuer.jwks())


def verifier(**options):
    return Verifier(ISSUER, EDITOR, keyset=keyset(), **options)


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


def member(engine, token, ls_aai_id=LS_AAI_ID, signing_key=None, result_key=RESULT_KEY):
    with db.writing(engine) as session:
        version = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:v", raw=b"{}", goal="g",
            proposer="m-01", members=[{"member": "m-01", "ls_aai_id": ls_aai_id}],
            nodes=[], datasets=[], output_rules={})
        repo.add_member_key(session, study_version_id=version.id, member="m-01",
                            ls_aai_id=ls_aai_id, signing_key=signing_key or SIGNING_KEY,
                            result_key=result_key,
                            binding_token=token)
        return version.id


def check(engine, version, chosen=None):
    with db.writing(engine) as session:
        count = identity.check_study_version(session, version, chosen or verifier())
    with db.reading(engine) as session:
        return count, repo.member_keys(session, version)[0]


class Key:
    def __init__(self, token, ls_aai_id=LS_AAI_ID, signing_key=None,
                 result_key=RESULT_KEY):
        self.binding_token = token
        self.ls_aai_id = ls_aai_id
        self.signing_key = dict(SIGNING_KEY) if signing_key is None else signing_key
        self.result_key = result_key


def refused(token, *, chosen=None, **key):
    with pytest.raises(IdentityError) as caught:
        identity.check_token(Key(token, **key), chosen or verifier())
    return str(caught.value)


def test_a_good_token_passes_every_check():
    assert identity.check_token(Key(LOGIN.token()), verifier())["name"] == "Ana Costa"


def test_a_token_from_another_key_is_refused():
    assert refused(Issuer().token()) == identity.NOT_VALID


def test_a_key_nobody_published_is_refused():
    assert refused(Issuer(kid="other").token()) == identity.NOT_VALID


def test_another_issuer_is_refused():
    assert refused(LOGIN.token(iss="https://evil.test")) == identity.NOT_VALID


def test_a_token_made_for_another_service_is_refused():
    assert refused(LOGIN.token(aud="another-client")) == identity.NOT_VALID


def test_a_token_with_no_audience_is_refused():
    assert refused(LOGIN.token(aud=None)) == identity.NOT_VALID


def test_an_expired_token_says_so():
    token = LOGIN.token(exp=int(time.time()) - 3600)
    assert refused(token) == identity.EXPIRED


def test_a_token_with_no_expiry_is_refused():
    assert refused(LOGIN.token(exp=None)) == identity.NOT_VALID


def test_a_token_about_someone_else_is_refused():
    assert refused(LOGIN.token(sub="other@lifescience-ri.eu")) == identity.WRONG_PERSON


def test_a_token_with_no_subject_is_refused():
    assert refused(LOGIN.token(sub=None)) == identity.WRONG_PERSON


def test_an_unsigned_token_is_refused():
    token = jwt.encode({"iss": ISSUER, "aud": EDITOR, "sub": LS_AAI_ID,
                        "exp": int(time.time()) + 300}, key=None, algorithm="none",
                       headers={"kid": "k1"})
    assert refused(token) == identity.NOT_VALID


def test_rubbish_is_refused():
    assert refused("not a token") == identity.NOT_VALID


def test_keys_that_cannot_be_fetched_are_a_reason_not_a_crash():
    def down(url):
        raise OSError("no route")
    chosen = Verifier(ISSUER, EDITOR,
                      keyset=passports.KeySet("https://login.test/jwks", fetch=down))
    assert refused(LOGIN.token(), chosen=chosen) == identity.NO_KEYS


def test_the_vector_gives_its_bytes_and_its_nonce():
    assert identity.member_keys_bytes(SIGNING_KEY, RESULT_KEY) == CANONICAL.encode()
    assert identity.member_keys_nonce(SIGNING_KEY, RESULT_KEY) == NONCE


def test_the_vector_passes_the_check():
    identity.check_nonce({"nonce": NONCE}, Key(None))


def test_the_check_is_the_default():
    assert Verifier(ISSUER, EDITOR).nonce_check is identity.check_nonce


def _with(**changes):
    return {**SIGNING_KEY, **changes}


def _one_byte_changed(coordinate):
    raw = bytearray(base64.urlsafe_b64decode(coordinate + "="))
    raw[5] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()


@pytest.mark.parametrize("signing_key", [
    _with(kid="k1"), _with(alg="ES256"), _with(use="sig")],
    ids=["kid", "alg", "use"])
def test_an_extra_jwk_member_is_refused(signing_key):
    assert refused(LOGIN.token(), signing_key=signing_key) == identity.MALFORMED_KEYS


def test_an_uppercase_result_key_is_refused():
    assert refused(LOGIN.token(), result_key=RESULT_KEY.upper()) == \
        identity.MALFORMED_KEYS


def _nonce_of(document):
    raw = rfc8785.dumps(document)
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()


def _document(**changes):
    document = {"v": identity.MEMBER_KEYS_VERSION, "result_key": RESULT_KEY,
                "signing_key": dict(SIGNING_KEY)}
    document.update(changes)
    return {name: value for name, value in document.items() if value is not None}


def test_a_nonce_made_without_v_is_refused():
    nonce = _nonce_of(_document(v=None))
    assert refused(LOGIN.token(nonce=nonce)) == identity.WRONG_NONCE


def test_a_nonce_made_with_another_v_is_refused():
    nonce = _nonce_of(_document(v="biochef.member-keys.v2"))
    assert refused(LOGIN.token(nonce=nonce)) == identity.WRONG_NONCE


def test_one_byte_changed_in_x_is_refused():
    changed = _with(x=_one_byte_changed(SIGNING_KEY["x"]))
    assert refused(LOGIN.token(), signing_key=changed) == identity.WRONG_NONCE


def test_the_two_keys_swapped_are_refused():
    assert refused(LOGIN.token(), signing_key=RESULT_KEY,
                   result_key=SIGNING_KEY) == identity.MALFORMED_KEYS


def test_a_nonce_for_the_keys_swapped_is_refused():
    nonce = _nonce_of(_document(result_key=SIGNING_KEY, signing_key=RESULT_KEY))
    assert refused(LOGIN.token(nonce=nonce)) == identity.WRONG_NONCE


def test_a_nonce_for_another_members_keys_is_refused():
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    coordinate = lambda n: base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(
        b"=").decode()
    keys = _with(x=coordinate(other.x), y=coordinate(other.y))
    nonce = identity.member_keys_nonce(keys, RESULT_KEY)
    assert refused(LOGIN.token(nonce=nonce)) == identity.WRONG_NONCE


@pytest.mark.parametrize("coordinate", [
    SIGNING_KEY["x"][1:],                        # a leading zero dropped
    SIGNING_KEY["x"] + "=",                      # padded
    SIGNING_KEY["x"][:-1] + "t",                 # same bytes, other last character
    SIGNING_KEY["x"].replace("_", "/"),          # base64, not base64url
], ids=["leading-zero-dropped", "padded", "non-canonical", "not-url-safe"])
def test_a_coordinate_not_in_its_one_form_is_refused(coordinate):
    assert refused(LOGIN.token(), signing_key=_with(x=coordinate)) == \
        identity.MALFORMED_KEYS


@pytest.mark.parametrize("signing_key", [
    _with(kty="OKP"), _with(crv="P-384")], ids=["kty", "crv"])
def test_a_key_of_another_type_is_refused(signing_key):
    assert refused(LOGIN.token(), signing_key=signing_key) == identity.MALFORMED_KEYS


@pytest.mark.parametrize("nonce", [None, NONCE.lower(), NONCE + "=", " " + NONCE],
                         ids=["missing", "case", "padded", "space"])
def test_the_nonce_is_compared_as_an_exact_string(nonce):
    assert refused(LOGIN.token(nonce=nonce)) == identity.WRONG_NONCE


def test_a_valid_token_verifies(engine):
    count, key = check(engine, member(engine, LOGIN.token()))
    assert count == 1
    assert key.identity_verified is True and key.identity_error is None


def test_a_wrong_nonce_is_recorded_and_leaves_no_name(engine):
    count, key = check(engine, member(engine, LOGIN.token(nonce=NONCE[::-1])))
    assert count == 0
    assert key.identity_verified is False
    assert key.identity_error == identity.WRONG_NONCE
    assert key.display_name is None


def test_a_failed_check_records_why_and_leaves_no_name(engine):
    _, key = check(engine, member(engine, LOGIN.token(aud="another-client")))
    assert key.identity_error == identity.NOT_VALID and key.display_name is None


def test_the_name_comes_from_the_token(engine):
    count, key = check(engine, member(engine, LOGIN.token()))
    assert count == 1
    assert (key.identity_verified, key.display_name) == (True, "Ana Costa")


def test_a_token_with_no_name_verifies_without_one(engine):
    _, key = check(engine, member(engine, LOGIN.token(name=None)))
    assert key.identity_verified is True and key.display_name is None


def test_a_later_failure_takes_the_name_away(engine):
    """The keys are fixed with the study version, so a later check fails for a
    reason outside them: here LS Login no longer publishes the token's key."""
    version = member(engine, LOGIN.token())
    _, key = check(engine, version)
    assert key.identity_verified is True and key.display_name == "Ana Costa"
    rotated = Verifier(ISSUER, EDITOR, keyset=keyset(Issuer()))
    _, key = check(engine, version, rotated)
    assert key.identity_verified is False and key.display_name is None


def test_a_version_with_a_failed_member_cannot_be_approved(engine):
    from store.errors import IdentityNotVerified
    version = member(engine, LOGIN.token(nonce=NONCE[::-1]))
    check(engine, version)
    with pytest.raises(IdentityNotVerified):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version)


@pytest.mark.parametrize("issuer,client", [("", EDITOR), (ISSUER, "")])
def test_both_settings_are_required(issuer, client):
    with pytest.raises(IdentityError, match="both required"):
        Verifier(issuer, client)


def test_the_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("BIOCHEF_LS_LOGIN_ISSUER", ISSUER)
    monkeypatch.setenv("BIOCHEF_EDITOR_CLIENT_ID", EDITOR)
    chosen = Verifier.from_settings()
    assert (chosen.issuer, chosen.client_id) == (ISSUER, EDITOR)


def test_the_keys_are_found_through_the_issuer(monkeypatch):
    monkeypatch.setattr(passports, "jwks_url_for",
                        lambda issuer: f"{issuer}/jwks")
    assert Verifier(ISSUER, EDITOR).keyset.url == f"{ISSUER}/jwks"


def test_an_issuer_that_cannot_be_reached_is_a_reason(monkeypatch):
    def down(issuer):
        raise OSError("no route")
    monkeypatch.setattr(passports, "jwks_url_for", down)
    assert refused(LOGIN.token(), chosen=Verifier(ISSUER, EDITOR)) == identity.NO_KEYS
