"""Egress in both modes, with fakes (section 6.5)."""

import base64
import hashlib
import io
import json
import tarfile
from datetime import timedelta

import pyrage
import pytest

import audit
from node import age, decisions, egress
from node.keys import generate
from node.policy import FakePolicyEngine
from store import db, repo
from store.errors import Conflict, IllegalTransition
from store.states import EgressState, SubjectType, Verdict
from tests.store_support import NOW, RULES, add_version, fresh_database

KEYS = generate()
NODE = "node-a"
FILES = [{"file": "summary.tsv", "size": 10, "verdict": None, "reason": None},
         {"file": "log.txt", "size": 10, "verdict": None, "reason": None},
         {"file": "big.tsv", "size": 5000, "verdict": None, "reason": None}]
RESULT = pyrage.x25519.Identity.generate()
KEEP = 3600


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    with db.writing(bind) as session:
        version = add_version(session)
        key = repo.member_keys(session, version.id)[0]
        key.result_key = str(RESULT.to_public())
        repo.add_request(session, request_id="req_1", study_id=version.study_id,
                         study_version=1, study_digest=version.digest,
                         workflow_digest="sha256:w", member="m-01", node=NODE,
                         datasets=[], not_after=NOW, payload_digest="sha256:p",
                         raw=b"{}", now=NOW)
        repo.set_verdict(session, "req_1", Verdict.ACCEPTED, run_id="run_1")
        repo.save_run(session, "run_1", state="COMPLETE", request_id="req_1",
                      workspace_path="/tmp/x", egress_files=FILES)
    return bind


def start(engine, mode, airlock=None):
    with db.writing(engine) as session:
        egress.start(session, "run_1", FakePolicyEngine(), airlock, mode=mode, now=NOW)


def run_of(engine):
    with db.reading(engine) as session:
        return repo.get_run(session, "run_1")


def verdicts(engine):
    return {row["file"]: row["verdict"] for row in run_of(engine).egress_files}


def test_the_rules_judge_every_file(engine):
    start(engine, egress.MANUAL)
    assert verdicts(engine) == {"summary.tsv": "pass", "log.txt": "held_back",
                                "big.tsv": "held_back"}


def test_a_file_the_engine_says_nothing_about_stays(engine):
    class Partial(FakePolicyEngine):
        def check(self, run, rules):
            return super().check(run, rules)[1:]
    with db.writing(engine) as session:
        egress.start(session, "run_1", Partial(), None, mode=egress.MANUAL, now=NOW)
    assert verdicts(engine)["summary.tsv"] == "held_back"


def test_manual_mode_waits_for_a_person(engine):
    start(engine, egress.MANUAL)
    assert run_of(engine).egress_state == "awaiting_review"


def test_airlock_mode_hands_over_passing_files_only(engine):
    airlock = egress.FakeAirlock()
    start(engine, egress.AIRLOCK, airlock)
    run = run_of(engine)
    assert run.egress_state == "awaiting_airlock"
    assert airlock.submitted[run.airlock_ticket] == ["summary.tsv"]
    assert run.airlock_at == NOW


def answered(engine, answer):
    airlock = egress.FakeAirlock()
    start(engine, egress.AIRLOCK, airlock)
    airlock.answers[run_of(engine).airlock_ticket] = answer
    with db.writing(engine) as session:
        egress.poll_airlock(session, airlock, now=NOW, keep_seconds=KEEP)
    return run_of(engine)


def test_the_airlock_can_say_nothing_yet(engine):
    assert answered(engine, "pending").egress_state == "awaiting_airlock"


def test_the_airlock_can_release(engine):
    run = answered(engine, "released")
    assert run.egress_state == "released" and run.retain_until is None


def test_the_airlock_can_withhold_and_the_clock_starts(engine):
    run = answered(engine, "withheld")
    assert run.egress_state == "withheld"
    assert run.retain_until == NOW + timedelta(seconds=KEEP)


def review(engine, act, user="j.silva", **options):
    start(engine, egress.MANUAL)
    with db.writing(engine) as session:
        return act(session, KEYS, node=NODE, run_id="run_1", user=user, now=NOW,
                   **options).id


def test_a_release_is_signed_and_names_the_user(engine):
    decision = review(engine, egress.release, "a.costa")
    with db.reading(engine) as session:
        row = repo.get_decision(session, decision)
    signed = decisions.verify(row.object_raw, row.signature, KEYS.signing.public_key())
    assert (signed["action"], signed["user"], signed["subject_type"]) == (
        "release", "a.costa", "egress")
    assert run_of(engine).egress_state == "released"


def test_a_release_does_not_start_the_clock(engine):
    review(engine, egress.release)
    assert run_of(engine).retain_until is None


def test_withholding_is_signed_and_starts_the_clock(engine):
    review(engine, egress.withhold, keep_seconds=KEEP)
    run = run_of(engine)
    assert run.egress_state == "withheld"
    assert run.retain_until == NOW + timedelta(seconds=KEEP)
    with db.reading(engine) as session:
        assert repo.decisions_about(session, SubjectType.EGRESS, "run_1")[0].action == "withhold"


def test_nothing_can_be_released_before_the_rules_ran(engine):
    with pytest.raises(IllegalTransition):
        with db.writing(engine) as session:
            egress.release(session, KEYS, node=NODE, run_id="run_1", user="j.silva")


def test_a_release_cannot_skip_the_airlock(engine):
    start(engine, egress.AIRLOCK, egress.FakeAirlock())
    assert run_of(engine).egress_state == "awaiting_airlock"
    with db.writing(engine) as session:
        egress.release(session, KEYS, node=NODE, run_id="run_1", user="j.silva")
    # The state machine allows it from either wait: the page is what refuses it
    # in airlock mode, and tests/test_ui_run.py pins that.
    assert run_of(engine).egress_state == "released"


def test_a_decision_needs_the_trail(engine, monkeypatch):
    start(engine, egress.MANUAL)
    monkeypatch.delenv("BIOCHEF_AUDIT_LOG")
    with pytest.raises(decisions.DecisionError):
        with db.writing(engine) as session:
            egress.release(session, KEYS, node=NODE, run_id="run_1", user="j.silva")
    assert run_of(engine).egress_state == "awaiting_review"


def undo(engine, decision):
    with db.writing(engine) as session:
        egress.undo_review(session, repo.get_decision(session, decision), user="a.costa")


def test_a_release_can_be_undone(engine, tmp_path):
    undo(engine, review(engine, egress.release))
    assert run_of(engine).egress_state == "awaiting_review"
    assert audit.read(str(tmp_path / "audit.jsonl"))[-1]["event"] == "node.decision.undone"


def test_a_withholding_can_be_undone_and_the_clock_stops(engine):
    undo(engine, review(engine, egress.withhold, keep_seconds=KEEP))
    run = run_of(engine)
    assert run.egress_state == "awaiting_review" and run.retain_until is None


def test_a_posted_decision_cannot_be_undone(engine):
    decision = review(engine, egress.release)
    with db.writing(engine) as session:
        repo.mark_posted(session, decision, now=NOW)
    with pytest.raises(Conflict, match="already been sent"):
        undo(engine, decision)


def test_a_sent_result_cannot_be_undone(engine):
    decision = review(engine, egress.release)
    with db.writing(engine) as session:
        repo.set_egress_state(session, "run_1", EgressState.SENT, NOW)
    with pytest.raises(Conflict, match="moved on"):
        undo(engine, decision)


class Workspace:
    def open_read(self, name):
        return io.BytesIO(f"contents of {name}".encode())


def send(engine, coordinator=None, workspace=Workspace()):
    coordinator = coordinator or egress.FakeCoordinator()
    with db.writing(engine) as session:
        egress.send_released(session, KEYS, coordinator, lambda run: workspace,
                             node=NODE, now=NOW, keep_seconds=KEEP)
    return coordinator


def released(engine):
    review(engine, egress.release)
    return send(engine).posted[0]


def test_a_release_is_sent_and_the_clock_starts(engine):
    released(engine)
    run = run_of(engine)
    assert run.egress_state == "sent"
    assert run.retain_until == NOW + timedelta(seconds=KEEP)


def test_only_passing_files_are_in_the_result(engine):
    document = released(engine)
    box = base64.b64decode(document["ciphertext"])
    archive = tarfile.open(fileobj=io.BytesIO(pyrage.decrypt(box, [RESULT])))
    assert archive.getnames() == ["summary.tsv"]


def test_the_result_is_encrypted_for_the_requester_only(engine):
    document = released(engine)
    box = base64.b64decode(document["ciphertext"])
    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(box, [pyrage.x25519.Identity.generate()])


def test_the_statement_follows_the_contract_and_is_signed(engine):
    document = released(engine)
    statement = document["statement"]
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://biochef.app/partial/v1"
    assert statement["predicate"]["request_id"] == "req_1"
    digest = hashlib.sha256(b"contents of summary.tsv").hexdigest()
    assert statement["subject"] == [{"name": "summary.tsv", "digest": {"sha256": digest}}]
    data = decisions.canonical(statement)
    KEYS.signing.public_key().verify(
        base64.urlsafe_b64decode(document["signature"] + "=" * (
            -len(document["signature"]) % 4)), data)


def test_a_result_is_sent_once(engine):
    review(engine, egress.release)
    first = send(engine)
    second = send(engine)
    assert len(first.posted) == 1 and second.posted == []


def test_a_workspace_that_is_gone_waits_rather_than_sends(engine):
    review(engine, egress.release)
    coordinator = egress.FakeCoordinator()
    with db.writing(engine) as session:
        egress.send_released(session, KEYS, coordinator, lambda run: None, node=NODE,
                             now=NOW)
    assert coordinator.posted == [] and run_of(engine).egress_state == "released"


def test_a_withheld_statement_has_no_subject_and_no_ciphertext(engine):
    with db.reading(engine) as session:
        request = repo.get_request(session, "req_1")
    document = egress.partial(KEYS, request, "age1x", egress.WITHHELD, node=NODE)
    assert document["ciphertext"] is None
    assert document["statement"]["subject"] == []
    assert document["decision"] == "withheld"


def test_the_fake_airlock_answers_pending_by_default():
    assert egress.FakeAirlock().status("anything") == "pending"


def test_the_rules_used_are_the_studys():
    class Row:
        egress_files = FILES
    verdicts = FakePolicyEngine().check(Row, RULES)
    assert [row.verdict for row in verdicts] == ["pass", "held_back", "held_back"]
