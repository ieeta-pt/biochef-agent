"""The ID token checks of section 7.3, one test each, and the closed nonce."""

import time

import jwt
import pytest
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
                  "iat": now, "name": "Ana Costa", "nonce": "n"}
        claims.update(overrides)
        claims = {name: value for name, value in claims.items() if value is not None}
        return jwt.encode(claims, self.key, algorithm="ES256",
                          headers={"kid": self.kid})


LOGIN = Issuer()


def keyset(issuer=LOGIN):
    return passports.KeySet("https://login.test/jwks", fetch=lambda url: issuer.jwks())


def verifier(**options):
    return Verifier(ISSUER, EDITOR, keyset=keyset(), **options)


def opened(claims, key):
    """Stands in for the nonce check, closed until its bytes are written."""


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


def member(engine, token, ls_aai_id=LS_AAI_ID):
    with db.writing(engine) as session:
        version = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:v", raw=b"{}", goal="g",
            proposer="m-01", members=[{"member": "m-01", "ls_aai_id": ls_aai_id}],
            nodes=[], datasets=[], output_rules={})
        repo.add_member_key(session, study_version_id=version.id, member="m-01",
                            ls_aai_id=ls_aai_id, signing_key={}, result_key="age1",
                            binding_token=token)
        return version.id


def check(engine, version, chosen=None):
    with db.writing(engine) as session:
        count = identity.check_study_version(session, version, chosen or verifier())
    with db.reading(engine) as session:
        return count, repo.member_keys(session, version)[0]


class Key:
    binding_token = None
    ls_aai_id = LS_AAI_ID

    def __init__(self, token, ls_aai_id=LS_AAI_ID):
        self.binding_token = token
        self.ls_aai_id = ls_aai_id


def refused(token, *, chosen=None, ls_aai_id=LS_AAI_ID):
    with pytest.raises(IdentityError) as caught:
        identity.check_token(Key(token, ls_aai_id),
                             chosen or verifier(nonce_check=opened))
    return str(caught.value)


def test_a_good_token_passes_every_check_but_the_nonce():
    assert identity.check_token(Key(LOGIN.token()), verifier(nonce_check=opened))[
        "name"] == "Ana Costa"


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
    chosen = Verifier(ISSUER, EDITOR, nonce_check=opened,
                      keyset=passports.KeySet("https://login.test/jwks", fetch=down))
    assert refused(LOGIN.token(), chosen=chosen) == identity.NO_KEYS


def test_the_nonce_stays_closed_by_default():
    assert Verifier(ISSUER, EDITOR).nonce_check is identity.nonce_closed
    assert refused(LOGIN.token(), chosen=verifier()) == identity.NONCE_UNDEFINED


def test_a_valid_token_still_counts_as_not_verified(engine):
    count, key = check(engine, member(engine, LOGIN.token()))
    assert count == 0
    assert key.identity_verified is False
    assert key.identity_error == identity.NONCE_UNDEFINED


def test_a_closed_nonce_leaves_no_name(engine):
    _, key = check(engine, member(engine, LOGIN.token()))
    assert key.display_name is None


def test_a_failed_check_records_why_and_leaves_no_name(engine):
    _, key = check(engine, member(engine, LOGIN.token(aud="another-client")))
    assert key.identity_error == identity.NOT_VALID and key.display_name is None


def test_when_the_nonce_opens_the_name_comes_from_the_token(engine):
    count, key = check(engine, member(engine, LOGIN.token()),
                       verifier(nonce_check=opened))
    assert count == 1
    assert (key.identity_verified, key.display_name) == (True, "Ana Costa")


def test_a_token_with_no_name_verifies_without_one(engine):
    _, key = check(engine, member(engine, LOGIN.token(name=None)),
                   verifier(nonce_check=opened))
    assert key.identity_verified is True and key.display_name is None


def test_a_later_failure_takes_the_name_away(engine):
    version = member(engine, LOGIN.token())
    check(engine, version, verifier(nonce_check=opened))
    _, key = check(engine, version)
    assert key.identity_verified is False and key.display_name is None


def test_a_version_with_a_failed_member_cannot_be_approved(engine):
    from store.errors import IdentityNotVerified
    version = member(engine, LOGIN.token())
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
