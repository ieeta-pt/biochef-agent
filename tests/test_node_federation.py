"""The signed site list: a bad signature, an older version or an expired list is
refused, and an expired one held makes requests wait (7.4)."""

import base64
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from node import federation
from node.federation import FederationError
from store import db, repo
from store.errors import Conflict, StaleList
from store.states import SubjectType
from tests.store_support import NOW, fresh_database

SIGNER = Ed25519PrivateKey.generate()
PUBLIC = SIGNER.public_key()


EXPIRES = NOW + timedelta(days=30)


def signed(version=14, nodes=("node-a", "node-b"), signer=SIGNER,
           expires_at=EXPIRES.isoformat()):
    document = {"version": version, "nodes": [{"node": n} for n in nodes]}
    if expires_at is not None:
        document["expires_at"] = expires_at
    raw = json.dumps(document).encode()
    return sign(raw, signer)


def sign(raw, signer=SIGNER):
    return raw, base64.urlsafe_b64encode(signer.sign(raw)).rstrip(b"=").decode()


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


def accept(engine, raw, signature):
    with db.writing(engine) as session:
        return federation.accept(session, raw, signature, PUBLIC, now=NOW)


def latest(engine):
    with db.reading(engine) as session:
        return repo.latest_federation_list(session)


def test_a_signed_list_is_kept_as_received(engine):
    raw, signature = signed()
    accept(engine, raw, signature)
    row = latest(engine)
    assert (row.version, row.raw, row.signature) == (14, raw, signature)
    assert len(row.nodes) == 2


def test_a_list_signed_by_someone_else_is_refused(engine):
    raw, signature = signed(signer=Ed25519PrivateKey.generate())
    with pytest.raises(FederationError, match="does not verify"):
        accept(engine, raw, signature)
    assert latest(engine) is None


def test_a_changed_list_is_refused(engine):
    raw, signature = signed()
    with pytest.raises(FederationError, match="does not verify"):
        accept(engine, raw.replace(b"node-b", b"node-x"), signature)


def test_rubbish_for_a_signature_is_a_refusal(engine):
    raw, _ = signed()
    with pytest.raises(FederationError, match="does not verify"):
        accept(engine, raw, "!!!")


def test_an_older_version_is_refused(engine):
    accept(engine, *signed(version=14))
    with pytest.raises(StaleList):
        accept(engine, *signed(version=13))
    assert latest(engine).version == 14


def test_a_newer_version_replaces_it(engine):
    accept(engine, *signed(version=14))
    accept(engine, *signed(version=15, nodes=("node-a",)))
    assert latest(engine).version == 15 and len(latest(engine).nodes) == 1


def test_the_same_list_again_is_nothing(engine):
    raw, signature = signed()
    accept(engine, raw, signature)
    accept(engine, raw, signature)
    assert latest(engine).version == 14


def test_a_different_list_under_the_same_version_is_refused(engine):
    accept(engine, *signed(nodes=("node-a",)))
    with pytest.raises(Conflict):
        accept(engine, *signed(nodes=("node-b",)))


@pytest.mark.parametrize("document", [b"not json", b"[]", b"{}", b'{"version": 1}',
                                      b'{"version": "1", "nodes": []}',
                                      b'{"version": true, "nodes": []}',
                                      b'{"version": 1, "nodes": {}}'])
def test_a_signed_document_that_is_no_list_is_refused(engine, document):
    signature = base64.urlsafe_b64encode(SIGNER.sign(document)).rstrip(b"=").decode()
    with pytest.raises(FederationError, match="not a list of nodes"):
        accept(engine, document, signature)


def write_pem(path, key):
    path.write_bytes(key.public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo))
    return str(path)


def test_the_key_comes_from_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_FEDERATION_KEY", write_pem(tmp_path / "k.pem", PUBLIC))
    raw, signature = signed()
    assert federation.check(raw, signature, federation.load_key(), NOW)["version"] == 14


def test_no_key_is_a_refusal(monkeypatch):
    monkeypatch.delenv("BIOCHEF_FEDERATION_KEY", raising=False)
    with pytest.raises(FederationError, match="not set"):
        federation.load_key()


def test_a_missing_or_bad_key_file_is_a_refusal(tmp_path):
    with pytest.raises(FederationError):
        federation.load_key(str(tmp_path / "none.pem"))
    (tmp_path / "bad.pem").write_text("not a key")
    with pytest.raises(FederationError):
        federation.load_key(str(tmp_path / "bad.pem"))


def test_a_key_of_another_type_is_refused(tmp_path):
    other = generate_private_key(SECP256R1()).public_key()
    with pytest.raises(FederationError, match="Ed25519"):
        federation.load_key(write_pem(tmp_path / "ec.pem", other))


# Expiry


@pytest.mark.parametrize("expires_at", [
    None, 5, "", "tomorrow", (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat()
], ids=["absent", "number", "empty", "words", "no zone"])
def test_a_list_that_does_not_say_when_it_expires_is_refused(engine, expires_at):
    with pytest.raises(FederationError, match="when it expires"):
        accept(engine, *signed(expires_at=expires_at))
    assert latest(engine) is None


@pytest.mark.parametrize("expires_at", [NOW, NOW - timedelta(seconds=1)],
                         ids=["this moment", "before"])
def test_an_expired_list_is_refused(engine, expires_at):
    with pytest.raises(FederationError, match="expired"):
        accept(engine, *signed(expires_at=expires_at.isoformat()))
    assert latest(engine) is None


def test_the_expiry_is_kept(engine):
    accept(engine, *signed())
    assert latest(engine).expires_at == EXPIRES


def test_a_newer_list_brings_a_new_expiry(engine):
    accept(engine, *signed(version=14, expires_at=(NOW + timedelta(hours=1)).isoformat()))
    accept(engine, *signed(version=15))
    with db.reading(engine) as session:
        assert repo.site_list_expired(session, NOW + timedelta(hours=2)) is False


# While the list held has expired


def deliver(engine, tmp_path, monkeypatch, at, request_id="req_1"):
    from node import requests
    from node.keys import generate
    from tests.fixtures.federation import Member, signed_request
    from node import age

    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    keys = generate()
    raw = signed_request(Member(), age.recipient(keys), request_id=request_id,
                         now=at)
    with db.writing(engine) as session:
        return requests.deliver(session, keys, raw, node="node-a",
                                visas=None, now=at)


def test_a_request_waits_unchecked_while_the_list_has_expired(engine, tmp_path,
                                                              monkeypatch):
    import audit
    from node.verify import Outcome

    accept(engine, *signed(expires_at=(NOW + timedelta(hours=1)).isoformat()))
    later = NOW + timedelta(hours=2)
    result = deliver(engine, tmp_path, monkeypatch, later)
    assert result.outcome is Outcome.HELD
    with db.reading(engine) as session:
        row = repo.get_request(session, "req_1")
        assert row.verdict is None, "kept, and not checked"
        assert repo.decisions_about(session, SubjectType.REQUEST, "req_1") == []
    held = [event for event in audit.read(str(tmp_path / "audit.jsonl"))
            if event["event"] == "node.request.held"]
    assert [(event["request_id"], event["reason"]) for event in held] == [
        ("req_1", "site list expired")]


def test_a_current_list_lets_requests_be_checked_again(engine, tmp_path, monkeypatch):
    from node.verify import Outcome

    accept(engine, *signed(version=14,
                           expires_at=(NOW + timedelta(hours=1)).isoformat()))
    later = NOW + timedelta(hours=2)
    assert deliver(engine, tmp_path, monkeypatch, later).outcome is Outcome.HELD
    with db.writing(engine) as session:
        federation.accept(session, *signed(
            version=15, expires_at=(later + timedelta(days=1)).isoformat()),
            PUBLIC, now=later)
    again = deliver(engine, tmp_path, monkeypatch, later)
    assert again.outcome is not Outcome.HELD
    with db.reading(engine) as session:
        assert repo.get_request(session, "req_1").verdict is not None


def test_the_node_card_says_the_list_has_expired(engine):
    from ui import views

    accept(engine, *signed(expires_at=(NOW + timedelta(hours=1)).isoformat()))
    with db.reading(engine) as session:
        current = views._federation_card(session, NOW)
        expired = views._federation_card(session, NOW + timedelta(hours=1))
    assert (current["kind"], current["headline"]) == ("ok", "Up to date")
    assert expired["kind"] == "bad"
    assert expired["headline"].startswith("Expired since")
    assert "requests wait unchecked" in expired["detail"]
