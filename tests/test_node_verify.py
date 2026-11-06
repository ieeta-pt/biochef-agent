"""One test per check of section 6.3, plus the join, with real keys and signatures."""

import json
import time
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

import audit
import passports
import pyrage
from node import age, decisions, requests, verify
from node.keys import generate
from node.requests import RequestError, VisaCheck
from store import db, repo
from store.errors import NotFound
from store.states import BindingSource
from store.states import RefusalCode as Code
from store.states import SubjectType
from store.states import Verdict as Outcome
from tests.fixtures.federation import (WORKFLOW, Member, signed_request,
                                       workflow_digest)
from tests.store_support import NOW, fresh_database

KEYS = generate()
MEMBER = Member()
NODE = "node-a"
ISSUER = "https://visas.test"
LS_AAI = "m-01@lifescience-ri.eu"
RECIPIENT = age.recipient(KEYS)
NO_VISA = VisaCheck(None)


class VisaIssuer:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def keyset(self):
        entry = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        document = {"keys": [{**entry, "kid": "v1", "alg": "ES256", "use": "sig"}]}
        return passports.KeySet("https://visas.test/jwks", fetch=lambda url: document)

    def visa(self, value="EGAD1", sub=LS_AAI, type="ControlledAccessGrants"):
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": sub, "exp": now + 300, "iat": now,
                  "ga4gh_visa_v1": {"type": type, "value": value}}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": "v1"})


VISAS = VisaIssuer()
REQUIRED = VisaCheck("ControlledAccessGrants", [ISSUER],
                     keyset_for=lambda issuer: VISAS.keyset())


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    with db.writing(bind) as session:
        version = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:st", raw=b"{}", goal="g",
            proposer="m-01", members=[{"member": "m-01"}], nodes=[],
            datasets=["EGAD1"], output_rules={}, now=NOW)
        repo.add_member_key(session, study_version_id=version.id, member="m-01",
                            ls_aai_id=LS_AAI, signing_key=MEMBER.jwk(),
                            result_key="age1", binding_token="t")
        session.flush()
        repo.mark_identity_verified(session, version.id, "m-01", {"sub": LS_AAI})
        repo.approve_study_version(session, version.id, now=NOW)
        workflow = repo.add_workflow_version(
            session, study_version_id=version.id, digest=workflow_digest(), raw=b"{}",
            steps=[], now=NOW)
        repo.approve_workflow_version(session, workflow.id, now=NOW)
        repo.bind_dataset(session, dataset_id="EGAD1",
                          source=BindingSource.CONFIG,
                          type="localpath", location="x", bound_by="config", now=NOW)
    return bind


def run(engine, visas=NO_VISA, signer=MEMBER, **fields):
    raw = signed_request(signer, RECIPIENT, now=NOW, **fields)
    with db.writing(engine) as session:
        row = requests.receive(session, raw, now=NOW)
        return requests.check(session, KEYS, row.request_id, node=NODE, visas=visas,
                              now=NOW)


def test_a_good_request_is_accepted_and_hands_back_the_payload(engine):
    result = run(engine)
    assert result.outcome is Outcome.ACCEPTED
    assert result.payload["workflow"] == WORKFLOW


def test_the_verdict_is_stored_without_the_payload(engine):
    run(engine)
    with db.reading(engine) as session:
        row = repo.get_request(session, "req_1")
    assert (row.verdict, row.failed_check) == ("accepted", None)


# 1 bad_signature


def test_1_a_signature_by_someone_else(engine):
    assert run(engine, signer=Member()).failed_check is Code.BAD_SIGNATURE


def test_1_a_version_this_node_does_not_hold(engine):
    assert run(engine, study_version=9).failed_check is Code.BAD_SIGNATURE


def test_1_a_member_the_version_does_not_list(engine):
    assert run(engine, member_id="m-99").failed_check is Code.BAD_SIGNATURE


def tampered(engine, change):
    raw = json.loads(signed_request(MEMBER, RECIPIENT, now=NOW))
    change(raw)
    with db.writing(engine) as session:
        row = requests.receive(session, json.dumps(raw).encode(), now=NOW)
        return requests.check(session, KEYS, row.request_id, node=NODE, visas=NO_VISA,
                              now=NOW)


def test_1_a_changed_request(engine):
    def change(raw):
        raw["job_request"]["datasets"] = ["EGAD2"]
    assert tampered(engine, change).failed_check is Code.BAD_SIGNATURE


@pytest.mark.parametrize("change", [
    lambda raw: raw["signature"].update(alg="none"),
    lambda raw: raw["signature"].update(sig="!!!"),
    lambda raw: raw["signature"].update(sig="QUJD"),
    lambda raw: raw.pop("signature"),
], ids=["alg", "rubbish", "short", "absent"])
def test_1_a_signature_that_is_not_one(engine, change):
    assert tampered(engine, change).failed_check is Code.BAD_SIGNATURE


# 2 wrong_node


def test_2_a_request_for_another_node(engine):
    assert run(engine, node="node-b").failed_check is Code.WRONG_NODE


# 3 study_not_approved


def test_3_a_withdrawn_study(engine):
    with db.writing(engine) as session:
        version = repo.study_version_named(session, "st", 1)
        repo.withdraw_study_version(session, version.id, now=NOW)
    assert run(engine).failed_check is Code.STUDY_NOT_APPROVED


def test_3_a_digest_that_is_not_this_versions(engine):
    assert run(engine, study_digest="sha256:other").failed_check is Code.STUDY_NOT_APPROVED


# 4 workflow_not_approved


def test_4_a_workflow_not_approved_here(engine):
    assert run(engine, digest="sha256:other").failed_check is Code.WORKFLOW_NOT_APPROVED


def test_4_a_withdrawn_workflow(engine):
    with db.writing(engine) as session:
        version = repo.study_version_named(session, "st", 1)
        workflow = repo.workflow_versions(session, version.id)[0]
        repo.withdraw_workflow_version(session, workflow.id, now=NOW)
    assert run(engine).failed_check is Code.WORKFLOW_NOT_APPROVED


# 5 expired


def test_5_an_expired_request(engine):
    assert run(engine, not_after=NOW - timedelta(seconds=1)).failed_check is Code.EXPIRED


def test_5_expiring_this_very_moment_is_expired(engine):
    assert run(engine, not_after=NOW).failed_check is Code.EXPIRED


# 6 seen before


def again(engine, first_fields=None, **second):
    first = run(engine, **{"request_id": "req_1", **(first_fields or {})})
    return first, run(engine, request_id="req_2", **second)


def test_6_a_repeat_of_an_accepted_request_joins(engine):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        requests.check(session, KEYS, "req_1", node=NODE, visas=NO_VISA, now=NOW)
        session.flush()
        repo.get_request(session, "req_1").run_id = "run_1"
    joined = run(engine, request_id="req_2")
    assert joined.outcome is Outcome.JOINED
    assert (joined.joined_request_id, joined.run_id) == ("req_1", "run_1")
    assert joined.payload is None


def test_6_a_repeat_of_a_join_joins_the_first(engine):
    _, second = again(engine)
    third = run(engine, request_id="req_3")
    assert third.joined_request_id == "req_1"


def test_6_a_join_is_not_refused_and_writes_no_decision(engine):
    again(engine)
    with db.reading(engine) as session:
        assert repo.decisions_about(session, SubjectType.REQUEST, "req_2") == []


def test_6_a_repeat_of_a_refused_request_is_a_replay(engine):
    plain = json.dumps({"visas": [], "workflow": {"different": True}}).encode()
    run(engine, plaintext=plain)
    assert run(engine, request_id="req_2", plaintext=plain).failed_check is Code.REPLAY


# 7 cannot_decrypt


def test_7_a_payload_for_another_node(engine):
    other = str(pyrage.x25519.Identity.generate().to_public())
    raw = signed_request(MEMBER, other, now=NOW)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        result = requests.check(session, KEYS, "req_1", node=NODE, visas=NO_VISA, now=NOW)
    assert result.failed_check is Code.CANNOT_DECRYPT


def test_7_a_digest_that_is_not_the_payloads(engine):
    assert run(engine, payload_digest="sha256:0").failed_check is Code.CANNOT_DECRYPT


def test_7_a_payload_in_another_scheme(engine):
    assert run(engine, enc="rsa").failed_check is Code.CANNOT_DECRYPT


def test_7_a_payload_that_is_not_json(engine):
    assert run(engine, plaintext=b"not json").failed_check is Code.CANNOT_DECRYPT


def test_7_rubbish_for_a_ciphertext(engine):
    def change(raw):
        raw["payload"]["ciphertext"] = "@@@"
    assert tampered(engine, change).failed_check is Code.CANNOT_DECRYPT


# 8 hash_mismatch


def test_8_tools_that_are_not_the_ones_approved(engine):
    changed = {**WORKFLOW, "nodes": []}
    assert run(engine, workflow=changed).failed_check is Code.HASH_MISMATCH


def test_8_a_payload_with_no_workflow(engine):
    plain = json.dumps({"visas": []}).encode()
    assert run(engine, plaintext=plain).failed_check is Code.HASH_MISMATCH


def test_8_a_float_in_the_workflow_is_a_mismatch_not_a_crash(engine):
    plain = json.dumps({"workflow": {"a": 1.5}}).encode()
    assert run(engine, plaintext=plain).failed_check is Code.HASH_MISMATCH


# 9 visa_missing


def test_9_no_visa_at_all(engine):
    assert run(engine, visas=REQUIRED).failed_check is Code.VISA_MISSING


def run_tokens(engine, tokens):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW, visas=tokens)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        return requests.check(session, KEYS, "req_1", node=NODE, visas=REQUIRED, now=NOW)


def test_9_the_right_visa_passes(engine):
    assert run_tokens(engine, [VISAS.visa()]).outcome is Outcome.ACCEPTED


def test_9_a_visa_for_another_dataset(engine):
    assert run_tokens(engine, [VISAS.visa(value="EGAD2")]).failed_check is Code.VISA_MISSING


def test_9_a_visa_issued_to_someone_else(engine):
    assert run_tokens(engine, [VISAS.visa(sub="x@y")]).failed_check is Code.VISA_MISSING


def test_9_a_visa_of_another_type(engine):
    assert run_tokens(engine, [VISAS.visa(type="AffiliationAndRole")]).failed_check is (
        Code.VISA_MISSING)


def test_9_a_visa_from_an_issuer_nobody_trusts(engine):
    forged = jwt.encode({"iss": "https://me.test", "sub": LS_AAI, "exp": int(time.time()) + 60,
                         "ga4gh_visa_v1": {"type": "ControlledAccessGrants",
                                           "value": "EGAD1"}},
                        VISAS.key, algorithm="ES256", headers={"kid": "v1"})
    assert run_tokens(engine, [forged]).failed_check is Code.VISA_MISSING


def test_9_every_dataset_needs_its_own_visa():
    allowed = REQUIRED.allow([VISAS.visa()], LS_AAI, ["EGAD1", "EGAD2"])
    assert allowed is False


def test_9_requiring_a_visa_with_no_issuer_is_refused():
    with pytest.raises(ValueError, match="BIOCHEF_PASSPORT_VISA_ISSUERS"):
        VisaCheck("ControlledAccessGrants")


def test_9_settings(monkeypatch):
    monkeypatch.setenv("BIOCHEF_PASSPORT_REQUIRE_VISA", "ControlledAccessGrants")
    monkeypatch.setenv("BIOCHEF_PASSPORT_VISA_ISSUERS", f"{ISSUER}, https://b.test")
    check = VisaCheck.from_settings()
    assert check.required_type == "ControlledAccessGrants"
    assert check.trusted_issuers == {ISSUER, "https://b.test"}


# 10 dataset_not_bound


def test_10_a_dataset_with_no_binding(engine):
    assert run(engine, datasets=("EGAD2",)).failed_check is Code.DATASET_NOT_BOUND


# The order, and what is recorded


def test_the_first_failure_stops_the_rest(engine):
    """Wrong node and expired: the earlier check is the one reported."""
    result = run(engine, node="node-b", not_after=NOW - timedelta(hours=1))
    assert result.failed_check is Code.WRONG_NODE


def test_every_verdict_writes_an_audit_event(engine, tmp_path):
    run(engine)
    run(engine, request_id="req_2", node="node-b")
    events = [row for row in audit.read(str(tmp_path / "audit.jsonl"))
              if row["event"] == "node.request.checked"]
    assert [(row["verdict"], row["failed_check"]) for row in events] == [
        ("accepted", None), ("refused", "wrong_node")]


def test_a_refusal_is_a_signed_decision(engine):
    run(engine, node="node-b")
    with db.reading(engine) as session:
        row = repo.decisions_about(session, SubjectType.REQUEST, "req_1")[0]
    signed = decisions.verify(row.object_raw, row.signature, KEYS.signing.public_key())
    assert (signed["action"], signed["reason"], signed["user"]) == (
        "refuse", "wrong_node", "agent")


def test_no_trail_no_verdict(engine, monkeypatch):
    monkeypatch.delenv("BIOCHEF_AUDIT_LOG")
    with pytest.raises(decisions.DecisionError, match="BIOCHEF_AUDIT_LOG"):
        run(engine)


def test_a_request_is_checked_once(engine):
    run(engine)
    with db.writing(engine) as session:
        assert requests.check(session, KEYS, "req_1", node=NODE, visas=NO_VISA) is None


def test_verify_changes_nothing(engine):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW)
    with db.writing(engine) as session:
        row = requests.receive(session, raw, now=NOW)
    with db.reading(engine) as session:
        row = repo.get_request(session, "req_1")
        verify.verify(row, session, KEYS, NOW, node=NODE, visas=NO_VISA)
    with db.reading(engine) as session:
        assert repo.get_request(session, "req_1").verdict is None


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b'{"job_request": {}}'])
def test_a_document_that_is_no_request_is_refused(engine, raw):
    with pytest.raises(RequestError):
        with db.writing(engine) as session:
            requests.receive(session, raw, now=NOW)


def test_checking_a_request_that_does_not_exist(engine):
    with pytest.raises(NotFound):
        with db.writing(engine) as session:
            requests.check(session, KEYS, "req_x", node=NODE, visas=NO_VISA, now=NOW)


# age


def test_the_nodes_recipient_is_the_form_age_uses():
    identity = pyrage.x25519.Identity.generate()
    assert str(identity.to_public()).startswith("age1")
    assert RECIPIENT.startswith("age1") and len(RECIPIENT) == 62
    pyrage.x25519.Recipient.from_str(RECIPIENT)


def test_a_round_trip_through_age():
    box = pyrage.encrypt(b"hello", [pyrage.x25519.Recipient.from_str(RECIPIENT)])
    assert age.decrypt(KEYS, box) == b"hello"
