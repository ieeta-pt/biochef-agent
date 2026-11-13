"""The signed site list: a bad signature or an older version is refused (7.4)."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from node import federation
from node.federation import FederationError
from store import db, repo
from store.errors import Conflict, StaleList
from tests.store_support import NOW, fresh_database

SIGNER = Ed25519PrivateKey.generate()
PUBLIC = SIGNER.public_key()


def signed(version=14, nodes=("node-a", "node-b"), signer=SIGNER):
    raw = json.dumps({"version": version, "nodes": [{"node": n} for n in nodes]}).encode()
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
    assert federation.check(raw, signature, federation.load_key())["version"] == 14


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
