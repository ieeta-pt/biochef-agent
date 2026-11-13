"""Decisions as a data manager makes them: rule 6.2.1, identity, the DAC, undo."""

from datetime import timedelta

import pytest

import audit
from node import decisions, review
from node.decisions import DecisionError
from node.keys import generate
from store import db, repo
from store.errors import (AlreadyPosted, Conflict, IdentityNotVerified,
                          IllegalTransition)
from store.states import Action, SubjectType, Verdict
from tests.store_support import NOW, add_version, add_workflow, fresh_database

KEYS = generate()
PUBLIC = KEYS.signing.public_key()
NODE = "node-a"
LATER = NOW + timedelta(hours=1)
LATEST = NOW + timedelta(hours=2)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    return fresh_database(tmp_path)[1]


@pytest.fixture
def trail(tmp_path):
    return tmp_path / "audit.jsonl"


def approve(session, version_id, user="j.silva", now=NOW):
    return review.approve_study(session, KEYS, node=NODE, version_id=version_id,
                                user=user, dac_confirmed=True, now=now)


@pytest.fixture
def two_versions(engine):
    """v1 approved with one approved and one pending workflow, v2 pending."""
    with db.writing(engine) as session:
        v1 = add_version(session, 1).id
        approve(session, v1)
        approved = add_workflow(session, v1, "sha256:w1").id
        review.approve_workflow(session, KEYS, node=NODE, workflow_id=approved,
                                user="j.silva", now=NOW)
        waiting = add_workflow(session, v1, "sha256:w2").id
        v2 = add_version(session, 2).id
    return v1, v2, approved, waiting


def states(engine, *versions):
    with db.reading(engine) as session:
        return [repo.get_study_version(session, row).state for row in versions]


def workflow_states(engine, *rows):
    with db.reading(engine) as session:
        return [repo.get_workflow_version(session, row).state for row in rows]


def about(engine, version):
    with db.reading(engine) as session:
        return repo.decisions_about(session, SubjectType.STUDY_VERSION, version)


# Rule 6.2.1


def test_approving_supersedes_the_approved_version(engine, two_versions):
    v1, v2, approved, waiting = two_versions
    with db.writing(engine) as session:
        approve(session, v2, now=LATER)
    assert states(engine, v1, v2) == ["superseded", "approved"]


def test_the_workflows_of_the_replaced_version_go_with_it(engine, two_versions):
    v1, v2, approved, waiting = two_versions
    with db.writing(engine) as session:
        approve(session, v2, now=LATER)
    assert workflow_states(engine, approved, waiting) == ["superseded", "superseded"]


def test_two_signed_decisions_land_with_the_change(engine, two_versions):
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        approve(session, v2, user="a.costa", now=LATER)
    approval, withdrawal = about(engine, v2)[0], about(engine, v1)[0]
    assert (approval.action, approval.dac_confirmed) == ("approve", True)
    assert (withdrawal.action, withdrawal.reason) == ("withdraw", "superseded")
    for row in (approval, withdrawal):
        signed = decisions.verify(row.object_raw, row.signature, PUBLIC)
        assert signed["user"] == "a.costa"
        assert signed["subject_id"] == row.subject_id


def test_a_failure_at_the_last_step_leaves_nothing_at_all(engine, two_versions,
                                                          monkeypatch):
    """The test of "in one transaction": the second decision cannot be written."""
    v1, v2, approved, waiting = two_versions
    real = audit.record
    calls = []

    def fail_second(event, **fields):
        calls.append(event)
        if len(calls) == 2:
            raise audit.AuditError("no space")
        return real(event, **fields)

    monkeypatch.setattr(audit, "record", fail_second)
    with pytest.raises(audit.AuditError):
        with db.writing(engine) as session:
            approve(session, v2, now=LATER)
    assert len(calls) == 2
    assert states(engine, v1, v2) == ["approved", "pending"]
    assert workflow_states(engine, approved, waiting) == ["approved", "pending"]
    assert about(engine, v2) == []
    assert [row.action for row in about(engine, v1)] == ["approve"]


def test_refusing_leaves_the_approved_version_alone(engine, two_versions):
    v1, v2, approved, _ = two_versions
    with db.writing(engine) as session:
        review.refuse_study(session, KEYS, node=NODE, version_id=v2, user="j.silva",
                            comment="too weak", now=LATER)
    assert states(engine, v1, v2) == ["approved", "refused"]
    assert workflow_states(engine, approved) == ["approved"]
    assert about(engine, v2)[0].comment == "too weak"


def test_a_failed_identity_cannot_be_approved(engine):
    with db.writing(engine) as session:
        version = add_version(session, verified=False).id
    with pytest.raises(IdentityNotVerified):
        with db.writing(engine) as session:
            approve(session, version)
    assert states(engine, version) == ["pending"]
    assert about(engine, version) == []


@pytest.mark.parametrize("confirmed", [False, None])
def test_the_dac_tick_is_required(engine, confirmed):
    with db.writing(engine) as session:
        version = add_version(session).id
    with pytest.raises(DecisionError, match="data access committee"):
        with db.writing(engine) as session:
            review.approve_study(session, KEYS, node=NODE, version_id=version,
                                 user="j.silva", dac_confirmed=confirmed)
    assert states(engine, version) == ["pending"]


def test_every_decision_reaches_the_trail_with_the_user(engine, two_versions, trail):
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        approve(session, v2, user="a.costa", now=LATER)
    events = audit.read(str(trail))[-2:]
    assert [row["user"] for row in events] == ["a.costa", "a.costa"]
    assert {row["subject_id"] for row in events} == {str(v1), str(v2)}


def test_an_approved_version_can_be_withdrawn(engine, two_versions):
    v1, *_ = two_versions
    with db.writing(engine) as session:
        review.withdraw_study(session, KEYS, node=NODE, version_id=v1, user="j.silva",
                              now=LATER)
    assert states(engine, v1) == ["withdrawn"]


def test_a_pending_version_cannot_be_withdrawn(engine, two_versions):
    _, v2, *_ = two_versions
    with pytest.raises(IllegalTransition):
        with db.writing(engine) as session:
            review.withdraw_study(session, KEYS, node=NODE, version_id=v2,
                                  user="j.silva")


def test_a_workflow_needs_an_approved_study_version(engine, two_versions):
    _, v2, *_ = two_versions
    with db.writing(engine) as session:
        workflow = add_workflow(session, v2, "sha256:w3").id
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            review.approve_workflow(session, KEYS, node=NODE, workflow_id=workflow,
                                    user="j.silva")
    assert workflow_states(engine, workflow) == ["pending"]


def test_withdrawing_a_workflow_is_a_signed_decision_of_its_own(engine, two_versions):
    v1, _, approved, _ = two_versions
    with db.writing(engine) as session:
        row = review.withdraw_workflow(session, KEYS, node=NODE, workflow_id=approved,
                                       user="a.costa", now=LATER)
        signed = decisions.verify(row.object_raw, row.signature, PUBLIC)
    assert signed["action"] == "withdraw" and signed["subject_type"] == "workflow_version"
    assert states(engine, v1) == ["approved"]


# Undo


def approved_second(engine, v2):
    with db.writing(engine) as session:
        return approve(session, v2, now=LATER).id


def test_undoing_an_approval_puts_every_state_back(engine, two_versions):
    v1, v2, approved, waiting = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        review.undo(session, decision, user="a.costa", now=LATEST)
    assert states(engine, v1, v2) == ["approved", "pending"]
    assert workflow_states(engine, approved, waiting) == ["approved", "pending"]


def test_undoing_an_approval_removes_both_decisions_and_says_who(engine, two_versions,
                                                                 trail):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        review.undo(session, decision, user="a.costa", now=LATEST)
    assert about(engine, v2) == []
    assert [row.action for row in about(engine, v1)] == ["approve"]
    events = audit.read(str(trail))[-2:]
    assert [row["event"] for row in events] == ["node.decision.undone"] * 2
    assert all(row["user"] == "a.costa" for row in events)


def test_a_version_can_be_approved_again_after_an_undo(engine, two_versions):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva", now=LATEST)
    with db.writing(engine) as session:
        approve(session, v2, now=LATEST + timedelta(hours=1))
    assert states(engine, v1, v2) == ["superseded", "approved"]


def test_a_first_approval_undoes_without_a_partner(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        decision = approve(session, version).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva")
    assert states(engine, version) == ["pending"]


def test_a_refusal_can_be_undone(engine, two_versions):
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        decision = review.refuse_study(session, KEYS, node=NODE, version_id=v2,
                                       user="j.silva", now=LATER).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva")
    assert states(engine, v1, v2) == ["approved", "pending"]


def test_a_withdrawal_can_be_undone(engine, two_versions):
    v1, *_ = two_versions
    with db.writing(engine) as session:
        decision = review.withdraw_study(session, KEYS, node=NODE, version_id=v1,
                                         user="j.silva", now=LATER).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva")
    assert states(engine, v1) == ["approved"]


@pytest.mark.parametrize("act,after", [
    (review.approve_workflow, "pending"), (review.refuse_workflow, "pending")])
def test_a_workflow_decision_can_be_undone(engine, two_versions, act, after):
    *_, waiting = two_versions
    with db.writing(engine) as session:
        decision = act(session, KEYS, node=NODE, workflow_id=waiting, user="j.silva",
                       now=LATER).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva")
    assert workflow_states(engine, waiting) == [after]


def test_withdrawing_a_workflow_can_be_undone(engine, two_versions):
    _, _, approved, _ = two_versions
    with db.writing(engine) as session:
        decision = review.withdraw_workflow(session, KEYS, node=NODE,
                                            workflow_id=approved, user="j.silva",
                                            now=LATER).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva")
    assert workflow_states(engine, approved) == ["approved"]


def test_nothing_that_left_the_node_can_be_undone(engine, two_versions):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        repo.mark_posted(session, decision, now=LATEST)
    with pytest.raises(AlreadyPosted):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")
    assert states(engine, v1, v2) == ["superseded", "approved"]


def test_an_approval_whose_withdrawal_left_cannot_be_undone(engine, two_versions):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        repo.mark_posted(session, about(engine, v1)[0].id, now=LATEST)
    with pytest.raises(Conflict, match="already been sent"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")
    assert states(engine, v1, v2) == ["superseded", "approved"]
    assert len(about(engine, v2)) == 1


def test_a_failed_undo_writes_nothing_to_the_trail(engine, two_versions, trail):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        repo.mark_posted(session, about(engine, v1)[0].id, now=LATEST)
    count = len(audit.read(str(trail)))
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")
    assert len(audit.read(str(trail))) == count


def test_the_withdrawal_an_approval_wrote_cannot_be_undone_alone(engine, two_versions):
    v1, v2, *_ = two_versions
    approved_second(engine, v2)
    with pytest.raises(Conflict, match="moved on"):
        with db.writing(engine) as session:
            review.undo(session, about(engine, v1)[0].id, user="j.silva")


def test_only_the_latest_decision_can_be_undone(engine, two_versions):
    _, v2, *_ = two_versions
    first = approved_second(engine, v2)
    with db.writing(engine) as session:
        review.withdraw_study(session, KEYS, node=NODE, version_id=v2, user="j.silva",
                              now=LATEST)
    with pytest.raises(Conflict, match="later decision"):
        with db.writing(engine) as session:
            review.undo(session, first, user="j.silva")


def test_an_approval_with_workflows_decided_under_it_stays(engine, two_versions):
    _, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        workflow = add_workflow(session, v2, "sha256:w9").id
        review.approve_workflow(session, KEYS, node=NODE, workflow_id=workflow,
                                user="j.silva", now=LATEST)
    with pytest.raises(Conflict, match="workflows have been decided"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def add_accepted_request(session, version, number, digest="sha256:w1"):
    repo.add_request(session, request_id="req_1", study_id=version.study_id,
                     study_version=number, study_digest=version.digest,
                     workflow_digest=digest, member="m-01", node=NODE,
                     datasets=[], not_after=LATEST, payload_digest="sha256:p",
                     raw=b"{}", now=NOW)
    repo.set_verdict(session, "req_1", Verdict.ACCEPTED, run_id="run_1")


def test_an_approval_something_ran_under_stays(engine, two_versions):
    _, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        add_accepted_request(session, repo.get_study_version(session, v2), 2)
    with pytest.raises(Conflict, match="requests have already run"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def test_a_workflow_approval_something_ran_under_stays(engine, two_versions):
    v1, _, _, waiting = two_versions
    with db.writing(engine) as session:
        decision = review.approve_workflow(session, KEYS, node=NODE, workflow_id=waiting,
                                           user="j.silva", now=LATER).id
        add_accepted_request(session, repo.get_study_version(session, v1), 1,
                             "sha256:w2")
    with pytest.raises(Conflict, match="requests have already run"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def test_a_withdrawal_cannot_bring_back_a_second_approved_version(engine, two_versions):
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        decision = review.withdraw_study(session, KEYS, node=NODE, version_id=v1,
                                         user="j.silva", now=LATER).id
        approve(session, v2, now=LATEST)
    with pytest.raises(Conflict, match="another version"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def test_a_workflow_cannot_return_to_a_replaced_study_version(engine, two_versions):
    _, v2, approved, _ = two_versions
    with db.writing(engine) as session:
        decision = review.withdraw_workflow(session, KEYS, node=NODE,
                                            workflow_id=approved, user="j.silva",
                                            now=LATER).id
        approve(session, v2, now=LATEST)
    with pytest.raises(Conflict, match="no longer approved"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def test_only_version_decisions_can_be_undone_here(engine):
    with db.writing(engine) as session:
        decision = decisions.decide(session, KEYS, node=NODE,
                                    subject_type=SubjectType.RUN, subject_id="r1",
                                    action=Action.CANCEL, user="j.silva", at=NOW).id
    with pytest.raises(Conflict, match="cannot be undone here"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")


def test_undo_says_why_not_before_anyone_tries(engine, two_versions):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.reading(engine) as session:
        row = repo.get_decision(session, decision)
        assert review.undo_refusal(session, row) is None
    with db.writing(engine) as session:
        repo.mark_posted(session, decision, now=LATEST)
    with db.reading(engine) as session:
        row = repo.get_decision(session, decision)
        assert "sent" in review.undo_refusal(session, row)


# What the store moves with a decision, and puts back with its undo


def test_withdrawing_a_version_takes_its_workflows_and_undo_returns_them(
        engine, two_versions):
    v1, _, approved, waiting = two_versions
    with db.writing(engine) as session:
        alone = add_workflow(session, v1, "sha256:w3").id
        review.approve_workflow(session, KEYS, node=NODE, workflow_id=alone,
                                user="j.silva", now=NOW)
        review.withdraw_workflow(session, KEYS, node=NODE, workflow_id=alone,
                                 user="j.silva", now=NOW + timedelta(minutes=5))
        decision = review.withdraw_study(session, KEYS, node=NODE, version_id=v1,
                                         user="j.silva", now=LATER).id
    assert workflow_states(engine, approved, waiting, alone) == [
        "withdrawn", "withdrawn", "withdrawn"]
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva", now=LATEST)
    assert states(engine, v1) == ["approved"]
    assert workflow_states(engine, approved, waiting, alone) == [
        "approved", "pending", "withdrawn"]


def test_undoing_an_approval_reopens_the_versions_it_superseded_while_waiting(
        engine, two_versions):
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        workflow = add_workflow(session, v2, "sha256:v2w").id
        v3 = add_version(session, 3).id
        decision = approve(session, v3, now=LATER).id
    assert states(engine, v1, v2, v3) == ["superseded", "superseded", "approved"]
    assert workflow_states(engine, workflow) == ["superseded"]
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva", now=LATEST)
    assert states(engine, v1, v2, v3) == ["approved", "pending", "pending"]
    assert workflow_states(engine, workflow) == ["pending"]


def test_undoing_an_approval_reopens_a_version_that_arrived_late(engine):
    with db.writing(engine) as session:
        v3 = add_version(session, 3).id
        decision = approve(session, v3).id
        v2 = add_version(session, 2).id
    assert states(engine, v2) == ["superseded"]
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva", now=LATER)
    assert states(engine, v2, v3) == ["pending", "pending"]


def test_undoing_an_approval_keeps_what_an_earlier_approval_superseded(engine):
    """v1 went when v2 was approved, and v2 is approved again after the undo."""
    with db.writing(engine) as session:
        v1 = add_version(session, 1).id
        v2 = add_version(session, 2).id
        approve(session, v2)
        v3 = add_version(session, 3).id
        decision = approve(session, v3, now=LATER).id
    with db.writing(engine) as session:
        review.undo(session, decision, user="j.silva", now=LATEST)
    assert states(engine, v1, v2, v3) == ["superseded", "approved", "pending"]


def test_a_refusal_cannot_come_back_under_a_newer_approval(engine, two_versions):
    """Pending again under v3, v2 could never be approved."""
    v1, v2, *_ = two_versions
    with db.writing(engine) as session:
        refusal = review.refuse_study(session, KEYS, node=NODE, version_id=v2,
                                      user="j.silva", now=LATER).id
        v3 = add_version(session, 3).id
        approve(session, v3, now=LATER)
    with db.reading(engine) as session:
        assert "v3 is approved now" in review.undo_refusal(
            session, repo.get_decision(session, refusal))
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            review.undo(session, refusal, user="j.silva", now=LATEST)
    assert states(engine, v2) == ["refused"]


def test_a_workflow_refusal_cannot_come_back_under_a_closed_version(
        engine, two_versions):
    v1, v2, _, waiting = two_versions
    with db.writing(engine) as session:
        refusal = review.refuse_workflow(session, KEYS, node=NODE,
                                         workflow_id=waiting, user="j.silva",
                                         now=LATER).id
        approve(session, v2, now=LATER)
    with pytest.raises(Conflict, match="no longer open"):
        with db.writing(engine) as session:
            review.undo(session, refusal, user="j.silva", now=LATEST)
    assert workflow_states(engine, waiting) == ["refused"]


def test_a_decision_on_its_way_cannot_be_undone(engine, two_versions):
    """Claimed by the poll and not yet answered: it may be at the coordinator."""
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    with db.writing(engine) as session:
        repo.claim_for_posting(session, decision, now=LATEST)
    with db.reading(engine) as session:
        assert review.undo_refusal(session, repo.get_decision(session, decision))
    with pytest.raises(AlreadyPosted):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")
    assert states(engine, v1, v2) == ["superseded", "approved"]


def test_an_approval_whose_withdrawal_is_on_its_way_stays(engine, two_versions):
    v1, v2, *_ = two_versions
    decision = approved_second(engine, v2)
    withdrawal = next(row.id for row in about(engine, v1)
                      if row.action == Action.WITHDRAW.value)
    with db.writing(engine) as session:
        repo.claim_for_posting(session, withdrawal, now=LATEST)
    with pytest.raises(Conflict, match="withdrawal it caused"):
        with db.writing(engine) as session:
            review.undo(session, decision, user="j.silva")
    assert states(engine, v1, v2) == ["superseded", "approved"]
