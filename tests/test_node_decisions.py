"""Signed decisions: the bytes, the signature, the store, the trail (part 2)."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from tests.store_support import NOW, add_version, fresh_database

import audit
from node import decisions
from node.decisions import DecisionError
from node.keys import generate
from store import db, repo
from store.errors import AlreadyPosted
from store.models import Decision, StudyVersion
from store.states import Action, SubjectType


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


@pytest.fixture
def trail(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(path))
    return path


KEYS = generate()
PUBLIC = KEYS.signing.public_key()


def document(**overrides):
    fields = {"node": "node-a", "subject_type": SubjectType.WORKFLOW_VERSION,
              "subject_id": 7, "action": Action.REFUSE, "user": "j.silva",
              "at": NOW}
    return decisions.build(**{**fields, **overrides})


def decide(session, **overrides):
    fields = {"node": "node-a", "subject_type": SubjectType.WORKFLOW_VERSION,
              "subject_id": 7, "action": Action.REFUSE, "user": "j.silva",
              "at": NOW}
    return decisions.decide(session, KEYS, **{**fields, **overrides})


# Canonical bytes


def test_the_bytes_do_not_depend_on_key_order():
    assert decisions.canonical({"b": 1, "a": 2}) == decisions.canonical({"a": 2, "b": 1})


def test_the_bytes_are_rfc_8785():
    """Sorted by UTF-16 code unit, no spaces, UTF-8, shortest escapes.

    U+1F600 is the pair D83D DE00, so it sorts before U+FB33 here, the opposite
    of code point order. That is the case a home-made canonical form gets wrong.
    """
    euro, smile, letter = chr(0x20AC), chr(0x1F600), chr(0xFB33)
    value = {letter: 3, euro: 1, smile: 2, "a": "é\n", "z": [True, None, -5]}
    expected = (f'{{"a":"é\\n","z":[true,null,-5],"{euro}":1,"{smile}":2,'
                f'"{letter}":3}}')
    assert decisions.canonical(value) == expected.encode()


@pytest.mark.parametrize("value", [1.5, {"a": 0.1}, [1, [2.0]], {"a": [{"b": 1e3}]}])
def test_a_float_cannot_be_signed(value):
    with pytest.raises(DecisionError, match="float"):
        decisions.canonical({"value": value})


def test_an_integer_json_cannot_hold_exactly_is_refused():
    with pytest.raises(DecisionError):
        decisions.canonical({"value": 2 ** 60})


# The decision object


def test_the_object_has_the_fields_of_the_spec():
    built = document(subject_hash="sha256:h", comment="no", reason="why")
    assert built == {
        "schema": "biochef.node-decision.v1", "node": "node-a",
        "subject_type": "workflow_version", "subject_id": "7",
        "subject_hash": "sha256:h", "action": "refuse", "user": "j.silva",
        "comment": "no", "at": "2026-11-05T12:00:00Z", "reason": "why"}


def test_the_time_is_utc_in_seconds():
    zone = timezone(timedelta(hours=5))
    moment = (NOW + timedelta(microseconds=999)).astimezone(zone)
    assert document(at=moment)["at"] == "2026-11-05T12:00:00Z"


def test_a_naive_time_is_refused():
    with pytest.raises(DecisionError, match="zone"):
        document(at=datetime(2026, 11, 5, 12, 0))


@pytest.mark.parametrize("user", ["", None])
def test_a_decision_says_who_made_it(user):
    with pytest.raises(DecisionError, match="who"):
        document(user=user)


def study_approval(**overrides):
    return document(subject_type=SubjectType.STUDY_VERSION, action=Action.APPROVE,
                    **overrides)


def test_a_study_approval_records_the_dac_confirmation():
    assert study_approval(dac_confirmed=True)["dac_confirmed"] is True


@pytest.mark.parametrize("value", [None, False])
def test_a_study_approval_needs_the_dac_confirmation(value):
    with pytest.raises(DecisionError, match="data access committee"):
        study_approval(dac_confirmed=value)


def test_no_other_decision_carries_the_dac_confirmation():
    with pytest.raises(DecisionError, match="study approvals only"):
        document(dac_confirmed=True)
    assert "dac_confirmed" not in document()


# Signature


def test_signing_and_verifying_round_trip():
    built = document()
    data, signature = decisions.sign(built, KEYS)
    assert decisions.verify(data, signature, PUBLIC) == built


def test_the_signature_is_base64url_without_padding():
    _, signature = decisions.sign(document(), KEYS)
    assert "=" not in signature and "+" not in signature and "/" not in signature
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    assert len(raw) == 64


def test_a_changed_object_does_not_verify():
    data, signature = decisions.sign(document(), KEYS)
    changed = data.replace(b"refuse", b"approv")
    with pytest.raises(DecisionError, match="does not verify"):
        decisions.verify(changed, signature, PUBLIC)


def test_a_changed_signature_does_not_verify():
    data, signature = decisions.sign(document(), KEYS)
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(DecisionError, match="does not verify"):
        decisions.verify(data, flipped, PUBLIC)


def test_another_key_does_not_verify():
    data, signature = decisions.sign(document(), KEYS)
    with pytest.raises(DecisionError, match="does not verify"):
        decisions.verify(data, signature, generate().signing.public_key())


def test_rubbish_for_a_signature_is_a_refusal_not_a_crash():
    data, _ = decisions.sign(document(), KEYS)
    with pytest.raises(DecisionError):
        decisions.verify(data, "!!!", PUBLIC)


def test_signed_bytes_that_are_not_canonical_are_refused():
    data = json.dumps(document(), indent=2).encode()
    with pytest.raises(DecisionError, match="not canonical"):
        decisions.verify(data, decisions.signature_of(data, KEYS), PUBLIC)


def test_signed_bytes_that_are_not_json_are_refused():
    data = b"not json"
    with pytest.raises(DecisionError, match="not JSON"):
        decisions.verify(data, decisions.signature_of(data, KEYS), PUBLIC)


def test_another_schema_is_refused():
    built = {**document(), "schema": "something.else"}
    data, signature = decisions.sign(built, KEYS)
    with pytest.raises(DecisionError, match="unknown schema"):
        decisions.verify(data, signature, PUBLIC)


# Stored and audited


def test_a_decision_is_stored_and_verifies_from_the_store(engine, trail):
    with db.writing(engine) as session:
        decision = decide(session, comment="too weak", reason="rules",
                          subject_hash="sha256:h").id
    with db.reading(engine) as session:
        row = session.get(Decision, decision)
    signed = decisions.verify(row.object_raw, row.signature, PUBLIC)
    assert (row.subject_type, row.subject_id, row.action, row.user) == (
        "workflow_version", "7", "refuse", "j.silva")
    assert (row.comment, row.reason, row.subject_hash, row.at) == (
        "too weak", "rules", "sha256:h", NOW)
    assert (signed["comment"], signed["reason"], signed["user"]) == (
        "too weak", "rules", "j.silva")
    assert row.posted_to_coordinator_at is None


def test_every_decision_writes_one_audit_event_with_the_user(engine, trail):
    with db.writing(engine) as session:
        first = decide(session, user="j.silva").id
        second = decide(session, user="a.costa", subject_id=8).id
    events = audit.read(str(trail))
    assert [event["event"] for event in events] == ["node.decision"] * 2
    assert [event["user"] for event in events] == ["j.silva", "a.costa"]
    assert [event["decision_id"] for event in events] == [first, second]
    assert all(event["signed"] is True for event in events)
    assert events[0]["signed_by"] == KEYS.signing_fingerprint
    assert (events[0]["subject_type"], events[0]["subject_id"],
            events[0]["action"]) == ("workflow_version", "7", "refuse")


def test_the_trail_does_not_carry_the_signature_or_the_key(engine, trail):
    with db.writing(engine) as session:
        row = decide(session)
    assert row.signature not in trail.read_text()


def test_a_decision_needs_a_trail(engine, monkeypatch):
    monkeypatch.delenv("BIOCHEF_AUDIT_LOG", raising=False)
    with pytest.raises(DecisionError, match="BIOCHEF_AUDIT_LOG"):
        with db.writing(engine) as session:
            decide(session)
    with db.reading(engine) as session:
        assert session.exec(select(Decision)).all() == []


def test_if_the_trail_cannot_be_written_the_decision_does_not_exist(
        engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "no" / "such" / "dir"))
    with pytest.raises(audit.AuditError):
        with db.writing(engine) as session:
            decide(session)
    with db.reading(engine) as session:
        assert session.exec(select(Decision)).all() == []


def test_a_state_change_and_its_decision_land_together(
        engine, tmp_path, monkeypatch):
    """An approval with no decision behind it would be an unsigned approval."""
    with db.writing(engine) as session:
        version = add_version(session).id
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "no" / "such" / "dir"))
    with pytest.raises(audit.AuditError):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version, now=NOW)
            decide(session, subject_type=SubjectType.STUDY_VERSION,
                   subject_id=version, action=Action.APPROVE, dac_confirmed=True)
    with db.reading(engine) as session:
        assert session.get(StudyVersion, version).state == "pending"


def test_a_study_approval_keeps_the_dac_confirmation(engine, trail):
    with db.writing(engine) as session:
        decision = decide(session, subject_type=SubjectType.STUDY_VERSION,
                          action=Action.APPROVE, dac_confirmed=True).id
    with db.reading(engine) as session:
        assert session.get(Decision, decision).dac_confirmed is True


# Undo


def test_undo_removes_the_decision_and_says_who(engine, trail):
    with db.writing(engine) as session:
        decision = decide(session).id
        decisions.undo(session, decision, user="a.costa")
        assert repo.unposted_decisions(session) == []
    events = audit.read(str(trail))
    assert [event["event"] for event in events] == [
        "node.decision", "node.decision.undone"]
    assert (events[1]["decision_id"], events[1]["user"]) == (decision, "a.costa")


def test_a_posted_decision_cannot_be_undone_and_leaves_no_event(engine, trail):
    with db.writing(engine) as session:
        decision = decide(session).id
        repo.mark_posted(session, decision, now=NOW)
    with pytest.raises(AlreadyPosted):
        with db.writing(engine) as session:
            decisions.undo(session, decision, user="a.costa")
    assert [event["event"] for event in audit.read(str(trail))] == ["node.decision"]


def test_an_undo_says_who_made_it(engine, trail):
    with db.writing(engine) as session:
        decision = decide(session).id
    with pytest.raises(DecisionError, match="who"):
        with db.writing(engine) as session:
            decisions.undo(session, decision, user="")
    with db.reading(engine) as session:
        assert session.get(Decision, decision) is not None


def test_an_undo_needs_a_trail(engine, trail, monkeypatch):
    with db.writing(engine) as session:
        decision = decide(session).id
    monkeypatch.delenv("BIOCHEF_AUDIT_LOG")
    with pytest.raises(DecisionError, match="BIOCHEF_AUDIT_LOG"):
        with db.writing(engine) as session:
            decisions.undo(session, decision, user="a.costa")
    with db.reading(engine) as session:
        assert session.get(Decision, decision) is not None
