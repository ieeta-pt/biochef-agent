"""One test per check of section 6.3, plus repeats, with real keys and signatures."""

import json
import time
from datetime import timedelta
from urllib.parse import quote

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
from sqlmodel import delete

from store.errors import NotFound
from store.models import Request
from store.states import BindingSource
from store.states import RefusalCode as Code
from node.verify import Outcome
from store.states import SubjectType
from tests.fixtures.federation import (WORKFLOW, Member, signed_request,
                                       workflow_digest)
from tests.store_support import NOW, fresh_database

KEYS = generate()
MEMBER = Member()
NODE = "node-a"
ISSUER = "https://visas.test"
LS_AAI = "m-01@lifescience-ri.eu"
VALUE = "https://dac.test/datasets/EGAD1"
RECIPIENT = age.recipient(KEYS)
MAX_AGE = 3600


class PassingVisas:
    """For the checks that are not about visas. Check 10 has its own tests."""

    def allow(self, tokens, ls_aai_id, values, now):
        return True


NO_VISA = PassingVisas()


class VisaIssuer:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def keyset(self):
        entry = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        document = {"keys": [{**entry, "kid": "v1", "alg": "ES256", "use": "sig"}]}
        return passports.KeySet("https://visas.test/jwks", fetch=lambda url: document)

    def visa(self, value=VALUE, sub=LS_AAI, type="ControlledAccessGrants",
             asserted=int(NOW.timestamp()) - 60, iss=ISSUER):
        now = int(time.time())
        visa = {"type": type, "value": value}
        if asserted is not None:
            visa["asserted"] = asserted
        claims = {"iss": iss, "sub": sub, "exp": now + 300, "iat": now,
                  "ga4gh_visa_v1": visa}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": "v1"})


VISAS = VisaIssuer()
REQUIRED = VisaCheck("ControlledAccessGrants", [ISSUER], MAX_AGE,
                     keyset_for=lambda issuer: VISAS.keyset())


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    with db.writing(bind) as session:
        version = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:st", raw=b"{}", goal="g",
            proposer="m-01", members=[{"member": "m-01"}], nodes=[],
            datasets=["EGAD1", "EGAD2"], output_rules={}, now=NOW)
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
                          type="localpath", location="x", visa_value=VALUE,
                          bound_by="config", now=NOW)
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


# 5 dataset_not_in_study


def test_5_a_dataset_the_study_does_not_name(engine):
    assert run(engine, datasets=("EGAD3",)).failed_check is Code.DATASET_NOT_IN_STUDY


def test_5_one_dataset_outside_the_study_among_several(engine):
    result = run(engine, datasets=("EGAD1", "EGAD3"))
    assert result.failed_check is Code.DATASET_NOT_IN_STUDY


def test_5_a_visa_and_a_binding_do_not_carry_a_request_across_studies(engine):
    """A member of two studies, with a visa for the second study's dataset, which
    is bound here: a request under the first study naming it is still refused."""
    other = "https://dac.test/datasets/EGAD7"
    with db.writing(engine) as session:
        repo.add_study_version(
            session, study_id="st2", version=1, digest="sha256:st2", raw=b"{}",
            goal="g", proposer="m-01", members=[{"member": "m-01"}], nodes=[],
            datasets=["EGAD7"], output_rules={}, now=NOW)
        repo.bind_dataset(session, dataset_id="EGAD7", source=BindingSource.CONFIG,
                          type="localpath", location="z", visa_value=other,
                          bound_by="config", now=NOW)
    raw = signed_request(MEMBER, RECIPIENT, now=NOW, datasets=("EGAD7",),
                         visas=[VISAS.visa(value=other)])
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        result = requests.check(session, KEYS, "req_1", node=NODE, visas=REQUIRED,
                                now=NOW)
    assert result.failed_check is Code.DATASET_NOT_IN_STUDY


def test_5_runs_before_the_payload_is_opened(engine):
    """Nothing in the payload is needed, so a request outside its study is
    refused without decrypting it."""
    result = run(engine, datasets=("EGAD3",), payload_digest="sha256:0")
    assert result.failed_check is Code.DATASET_NOT_IN_STUDY


# 6 expired


def test_6_an_expired_request(engine):
    assert run(engine, not_after=NOW - timedelta(seconds=1)).failed_check is Code.EXPIRED


def test_6_expiring_this_very_moment_is_expired(engine):
    assert run(engine, not_after=NOW).failed_check is Code.EXPIRED


# 7 seen before: the same request_id


def deliver(engine, visas=NO_VISA, signer=MEMBER, at=NOW, **fields):
    raw = signed_request(signer, RECIPIENT, now=NOW, **fields)
    with db.writing(engine) as session:
        return requests.deliver(session, KEYS, raw, node=NODE, visas=visas, now=at)


def stored(engine, request_id="req_1"):
    with db.reading(engine) as session:
        return repo.get_request(session, request_id)


def refusals(engine, request_id="req_1"):
    with db.reading(engine) as session:
        return repo.decisions_about(session, SubjectType.REQUEST, request_id)


def test_7_the_same_request_again_joins_its_run(engine):
    deliver(engine)
    with db.writing(engine) as session:
        repo.get_request(session, "req_1").run_id = "run_1"
    # Signed and encrypted again, so other bytes: the same signed request.
    later = NOW + timedelta(minutes=5)
    joined = deliver(engine, at=later)
    assert joined.outcome is Outcome.JOINED
    assert (joined.repeat_of, joined.run_id, joined.payload) == ("req_1", "run_1", None)
    row = stored(engine)
    assert (row.verdict, row.repeats, row.last_repeat_at) == ("accepted", 1, later)


def test_7_every_repeat_is_counted(engine):
    for _ in range(3):
        deliver(engine)
    assert stored(engine).repeats == 2


def test_7_a_repeat_writes_no_decision(engine):
    deliver(engine)
    deliver(engine)
    assert refusals(engine) == []


def test_7_a_repeat_of_a_refusal_gets_the_same_refusal(engine):
    deliver(engine, datasets=("EGAD2",))
    again = deliver(engine, datasets=("EGAD2",))
    assert (again.outcome, again.failed_check, again.repeat_of) == (
        Outcome.REFUSED, Code.DATASET_NOT_BOUND, "req_1")
    assert stored(engine).repeats == 1
    assert len(refusals(engine)) == 1


def test_7_the_refusal_stands_even_when_its_reason_has_gone(engine):
    deliver(engine, datasets=("EGAD2",))
    with db.writing(engine) as session:
        repo.bind_dataset(session, dataset_id="EGAD2", source=BindingSource.CONFIG,
                          type="localpath", location="y", bound_by="config", now=NOW)
    again = deliver(engine, datasets=("EGAD2",))
    assert again.failed_check is Code.DATASET_NOT_BOUND
    assert stored(engine).verdict == "refused"


def test_7_a_new_request_id_is_a_new_request_with_the_same_payload(engine):
    plain = json.dumps({"visas": [], "workflow": WORKFLOW}).encode()
    deliver(engine, plaintext=plain, datasets=("EGAD2",))
    fresh = deliver(engine, request_id="req_2", plaintext=plain)
    assert (fresh.outcome, fresh.repeat_of) == (Outcome.ACCEPTED, None)
    assert stored(engine, "req_2").verdict == "accepted"


def test_7_another_signed_request_under_the_same_id_is_a_replay(engine):
    deliver(engine)
    replay = deliver(engine, datasets=("EGAD1", "EGAD2"))
    assert (replay.outcome, replay.failed_check, replay.repeat_of) == (
        Outcome.REFUSED, Code.REPLAY, None)
    row = stored(engine)
    assert (row.verdict, row.datasets, row.repeats) == ("accepted", ["EGAD1"], 0)
    signed = refusals(engine)[0]
    assert (signed.action, signed.reason) == ("refuse", "replay")


def test_7_a_forgery_under_the_same_id_changes_nothing_and_signs_nothing(engine):
    deliver(engine)
    forged = deliver(engine, signer=Member())
    assert forged.failed_check is Code.BAD_SIGNATURE
    assert stored(engine).repeats == 0
    assert refusals(engine) == []


def test_7_after_not_after_a_repeat_is_expired_not_joined(engine):
    deliver(engine)
    late = deliver(engine, at=NOW + timedelta(hours=2))
    assert (late.failed_check, late.repeat_of) == (Code.EXPIRED, None)
    row = stored(engine)
    assert (row.verdict, row.repeats) == ("accepted", 0)
    assert refusals(engine) == []


def test_7_a_request_received_and_never_checked_is_checked_first(engine):
    with db.writing(engine) as session:
        requests.receive(session, signed_request(MEMBER, RECIPIENT, now=NOW), now=NOW)
    again = deliver(engine)
    assert again.outcome is Outcome.JOINED
    assert (stored(engine).verdict, stored(engine).repeats) == ("accepted", 1)


def test_7_every_repeat_writes_an_audit_event(engine, tmp_path):
    deliver(engine)
    deliver(engine)
    deliver(engine, datasets=("EGAD2",))
    events = [row for row in audit.read(str(tmp_path / "audit.jsonl"))
              if row["event"] == "node.request.checked"]
    assert [(row["verdict"], row["failed_check"], row.get("repeat_of")) for row in events
            ] == [("accepted", None, None), ("joined", None, "req_1"),
                  ("refused", "replay", None)]
    assert [row.get("repeat", False) for row in events] == [False, True, True]


def test_7_verify_on_a_repeat_before_its_first_check_is_refused(engine):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        delivery = repo.request_as_delivered(**requests._fields(raw), raw=raw, now=NOW)
        with pytest.raises(ValueError, match="checked before"):
            verify.verify(delivery, session, KEYS, NOW, node=NODE, visas=NO_VISA)


@pytest.mark.parametrize("raw", [b'{"job_request": {"request_id": ""}}',
                                 b'{"job_request": {"request_id": 5}}'])
def test_7_a_delivery_with_no_request_id_is_no_request(engine, raw):
    document = json.loads(signed_request(MEMBER, RECIPIENT, now=NOW))
    document["job_request"]["request_id"] = json.loads(raw)["job_request"]["request_id"]
    with pytest.raises(RequestError):
        with db.writing(engine) as session:
            requests.deliver(session, KEYS, json.dumps(document).encode(), node=NODE,
                             visas=NO_VISA, now=NOW)


# 8 cannot_decrypt


def test_8_a_payload_for_another_node(engine):
    other = str(pyrage.x25519.Identity.generate().to_public())
    raw = signed_request(MEMBER, other, now=NOW)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        result = requests.check(session, KEYS, "req_1", node=NODE, visas=NO_VISA, now=NOW)
    assert result.failed_check is Code.CANNOT_DECRYPT


def test_8_a_digest_that_is_not_the_payloads(engine):
    assert run(engine, payload_digest="sha256:0").failed_check is Code.CANNOT_DECRYPT


def test_8_a_payload_in_another_scheme(engine):
    assert run(engine, enc="rsa").failed_check is Code.CANNOT_DECRYPT


def test_8_a_payload_that_is_not_json(engine):
    assert run(engine, plaintext=b"not json").failed_check is Code.CANNOT_DECRYPT


def test_8_rubbish_for_a_ciphertext(engine):
    def change(raw):
        raw["payload"]["ciphertext"] = "@@@"
    assert tampered(engine, change).failed_check is Code.CANNOT_DECRYPT


# 9 hash_mismatch


def test_9_tools_that_are_not_the_ones_approved(engine):
    changed = {**WORKFLOW, "nodes": []}
    assert run(engine, workflow=changed).failed_check is Code.HASH_MISMATCH


def test_9_a_payload_with_no_workflow(engine):
    plain = json.dumps({"visas": []}).encode()
    assert run(engine, plaintext=plain).failed_check is Code.HASH_MISMATCH


def test_9_a_float_in_the_workflow_is_a_mismatch_not_a_crash(engine):
    plain = json.dumps({"workflow": {"a": 1.5}}).encode()
    assert run(engine, plaintext=plain).failed_check is Code.HASH_MISMATCH


# 10 visa_missing


def test_10_no_visa_at_all(engine):
    assert run(engine, visas=REQUIRED).failed_check is Code.VISA_MISSING


def run_tokens(engine, tokens):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW, visas=tokens)
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        return requests.check(session, KEYS, "req_1", node=NODE, visas=REQUIRED, now=NOW)


def test_10_the_right_visa_passes(engine):
    assert run_tokens(engine, [VISAS.visa()]).outcome is Outcome.ACCEPTED


def test_10_a_visa_for_another_dataset(engine):
    other = "https://dac.test/datasets/EGAD2"
    assert run_tokens(engine, [VISAS.visa(value=other)]).failed_check is Code.VISA_MISSING


def test_10_a_visa_carrying_the_dataset_id_is_not_the_visa_value(engine):
    assert run_tokens(engine, [VISAS.visa(value="EGAD1")]).failed_check is (
        Code.VISA_MISSING)


def test_10_the_comparison_is_exact(engine):
    for near in (VALUE + "/", VALUE.upper(), VALUE.replace("https", "http")):
        with db.writing(engine) as session:
            session.exec(delete(Request))
        assert run_tokens(engine, [VISAS.visa(value=near)]).failed_check is (
            Code.VISA_MISSING), near


def test_10_a_binding_with_no_visa_value_refuses_every_visa(engine):
    with db.writing(engine) as session:
        repo.dataset_binding(session, "EGAD1").visa_value = None
    assert run_tokens(engine, [VISAS.visa()]).failed_check is Code.VISA_MISSING


def test_10_a_visa_with_no_value_does_not_match_a_binding_with_none(engine):
    with db.writing(engine) as session:
        repo.dataset_binding(session, "EGAD1").visa_value = None
    assert run_tokens(engine, [VISAS.visa(value=None)]).failed_check is (
        Code.VISA_MISSING)


def test_10_an_unbound_dataset_is_left_to_check_11(engine):
    raw = signed_request(MEMBER, RECIPIENT, now=NOW, visas=[VISAS.visa()],
                         datasets=("EGAD1", "EGAD2"))
    with db.writing(engine) as session:
        requests.receive(session, raw, now=NOW)
        result = requests.check(session, KEYS, "req_1", node=NODE, visas=REQUIRED,
                                now=NOW)
    assert result.failed_check is Code.DATASET_NOT_BOUND


def test_10_a_visa_issued_to_someone_else(engine):
    assert run_tokens(engine, [VISAS.visa(sub="x@y")]).failed_check is Code.VISA_MISSING


def test_10_a_visa_of_another_type(engine):
    assert run_tokens(engine, [VISAS.visa(type="AffiliationAndRole")]).failed_check is (
        Code.VISA_MISSING)


def test_10_a_visa_from_an_issuer_nobody_trusts(engine):
    forged = jwt.encode({"iss": "https://me.test", "sub": LS_AAI, "exp": int(time.time()) + 60,
                         "ga4gh_visa_v1": {"type": "ControlledAccessGrants",
                                           "value": VALUE,
                                           "asserted": int(NOW.timestamp())}},
                        VISAS.key, algorithm="ES256", headers={"kid": "v1"})
    assert run_tokens(engine, [forged]).failed_check is Code.VISA_MISSING


def test_10_every_dataset_needs_its_own_visa():
    allowed = REQUIRED.allow([VISAS.visa()], LS_AAI,
                             [VALUE, "https://dac.test/datasets/EGAD2"], NOW)
    assert allowed is False


def test_10_a_missing_value_among_several_refuses():
    assert REQUIRED.allow([VISAS.visa()], LS_AAI, [VALUE, None], NOW) is False


def test_10_a_visa_as_old_as_the_limit_still_counts(engine):
    at_limit = VISAS.visa(asserted=int(NOW.timestamp()) - MAX_AGE)
    assert run_tokens(engine, [at_limit]).outcome is Outcome.ACCEPTED


def test_10_a_visa_older_than_the_limit(engine):
    old = VISAS.visa(asserted=int(NOW.timestamp()) - MAX_AGE - 1)
    assert run_tokens(engine, [old]).failed_check is Code.VISA_MISSING


@pytest.mark.parametrize("asserted", [None, "2026-11-05", 1.5, True],
                         ids=["absent", "text", "float", "bool"])
def test_10_a_visa_that_does_not_say_when_it_was_asserted(engine, asserted):
    if asserted is None:
        token = VISAS.visa(asserted=None)
    else:
        token = VISAS.visa(asserted=asserted)
    assert run_tokens(engine, [token]).failed_check is Code.VISA_MISSING


def linked(*pairs, sub=LS_AAI, **fields):
    value = ";".join(f"{quote(subject, safe='')},{quote(issuer, safe='')}"
                     for subject, issuer in pairs)
    return VISAS.visa(type="LinkedIdentities", value=value, sub=sub, **fields)


def test_10_a_visa_linked_to_the_member_counts(engine):
    """Issued to another of the member's identities, which the member's own
    LinkedIdentities visa names."""
    other = VISAS.visa(sub="old-id@elixir")
    tokens = [other, linked(("old-id@elixir", ISSUER))]
    assert run_tokens(engine, tokens).outcome is Outcome.ACCEPTED


def test_10_a_link_names_the_issuer_too(engine):
    other = VISAS.visa(sub="old-id@elixir")
    tokens = [other, linked(("old-id@elixir", "https://elsewhere.test"))]
    assert run_tokens(engine, tokens).failed_check is Code.VISA_MISSING


def test_10_a_link_held_by_someone_else_proves_nothing(engine):
    other = VISAS.visa(sub="old-id@elixir")
    tokens = [other, linked(("old-id@elixir", ISSUER), sub="x@y")]
    assert run_tokens(engine, tokens).failed_check is Code.VISA_MISSING


def test_10_a_link_older_than_the_limit_proves_nothing(engine):
    other = VISAS.visa(sub="old-id@elixir")
    stale = linked(("old-id@elixir", ISSUER),
                   asserted=int(NOW.timestamp()) - MAX_AGE - 1)
    assert run_tokens(engine, [other, stale]).failed_check is Code.VISA_MISSING


def test_10_a_link_from_an_issuer_nobody_trusts_proves_nothing(engine):
    other = VISAS.visa(sub="old-id@elixir")
    untrusted = linked(("old-id@elixir", ISSUER), iss="https://me.test")
    assert run_tokens(engine, [other, untrusted]).failed_check is Code.VISA_MISSING


@pytest.mark.parametrize("args, setting", [
    ((None, [ISSUER], MAX_AGE), "BIOCHEF_PASSPORT_REQUIRE_VISA"),
    (("ControlledAccessGrants", [], MAX_AGE), "BIOCHEF_PASSPORT_VISA_ISSUERS"),
    (("ControlledAccessGrants", [ISSUER], 0), "BIOCHEF_PASSPORT_VISA_MAX_AGE"),
    (("ControlledAccessGrants", [ISSUER], None), "BIOCHEF_PASSPORT_VISA_MAX_AGE"),
], ids=["no type", "no issuer", "no age", "age unset"])
def test_10_a_node_cannot_check_visas_half_configured(args, setting):
    with pytest.raises(ValueError, match=setting):
        VisaCheck(*args)


def test_10_settings(monkeypatch):
    monkeypatch.setenv("BIOCHEF_PASSPORT_REQUIRE_VISA", "ControlledAccessGrants")
    monkeypatch.setenv("BIOCHEF_PASSPORT_VISA_ISSUERS", f"{ISSUER}, https://b.test")
    monkeypatch.setenv("BIOCHEF_PASSPORT_VISA_MAX_AGE", "86400")
    check = VisaCheck.from_settings()
    assert check.required_type == "ControlledAccessGrants"
    assert check.trusted_issuers == {ISSUER, "https://b.test"}
    assert check.max_age == 86400


@pytest.mark.parametrize("value", [None, "", "a day", "-1"])
def test_10_settings_without_a_usable_age(monkeypatch, value):
    monkeypatch.setenv("BIOCHEF_PASSPORT_REQUIRE_VISA", "ControlledAccessGrants")
    monkeypatch.setenv("BIOCHEF_PASSPORT_VISA_ISSUERS", ISSUER)
    if value is None:
        monkeypatch.delenv("BIOCHEF_PASSPORT_VISA_MAX_AGE", raising=False)
    else:
        monkeypatch.setenv("BIOCHEF_PASSPORT_VISA_MAX_AGE", value)
    with pytest.raises(ValueError, match="BIOCHEF_PASSPORT_VISA_MAX_AGE"):
        VisaCheck.from_settings()


# 11 dataset_not_bound


def test_11_a_dataset_with_no_binding(engine):
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
