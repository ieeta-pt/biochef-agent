"""The rules the store keeps, table by table (agent UI, part 1)."""

import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from tests.store_support import (NOW, REPO_ROOT, add_decision, add_request,
                           add_version, add_workflow, fresh_database)

from store import db, repo
from store.errors import (AlreadyPosted, Conflict, IdentityNotVerified,
                          IllegalTransition, ImmutableField, NotFound,
                          StaleList)
from store.models import Decision, MemberKey, Request, StudyVersion
from store.states import (Action, BindingSource, EgressState, RefusalCode,
                          SubjectType, VersionState, Verdict)


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


def state_of(engine, version_id):
    with db.reading(engine) as session:
        return repo.get_study_version(session, version_id).state


# Signed bytes


def test_raw_comes_back_byte_for_byte(engine):
    """Key order, spacing and non-UTF-8 bytes: the signature covers all of it."""
    raw = b'{ "b":1,\n\t"a" : 2 }\xff\x00\xfe'
    with db.writing(engine) as session:
        row = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:x", raw=raw,
            goal="g", proposer="m", members=[], nodes=[], datasets=[],
            output_rules={}, now=NOW)
        version_id = row.id
    with db.reading(engine) as session:
        assert repo.get_study_version(session, version_id).raw == raw


@pytest.mark.parametrize("field,value", [("raw", b"other"), ("digest", "sha256:y"),
                                         ("version", 9)])
def test_a_received_version_cannot_be_rewritten(engine, field, value):
    with db.writing(engine) as session:
        version_id = add_version(session).id
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            setattr(session.get(StudyVersion, version_id), field, value)


def test_receiving_the_same_version_again_is_harmless(engine):
    with db.writing(engine) as session:
        first = add_version(session).id
        again = repo.add_study_version(
            session, study_id="st-01", version=1, digest="sha256:st-01-1",
            raw=b'{"v":1}', goal="g", proposer="m", members=[], nodes=[],
            datasets=[], output_rules={})
        assert again.id == first


def test_two_documents_under_one_version_are_refused(engine):
    with db.writing(engine) as session:
        add_version(session)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.add_study_version(
                session, study_id="st-01", version=1, digest="sha256:other",
                raw=b"other", goal="g", proposer="m", members=[], nodes=[],
                datasets=[], output_rules={})


# Times


def test_a_naive_datetime_is_refused(engine):
    with pytest.raises(Exception, match="naive"):
        with db.writing(engine) as session:
            add_request(session, received=datetime(2026, 11, 5, 12, 0))


def test_times_come_back_in_utc(engine):
    other_zone = NOW.astimezone(timezone(timedelta(hours=5)))
    with db.writing(engine) as session:
        add_request(session, received=other_zone)
    with db.reading(engine) as session:
        received = repo.get_request(session, "req_1").received_at
    assert received == NOW and received.utcoffset() == timedelta(0)


# One approved version


def test_approving_supersedes_the_approved_version_and_its_workflows(engine):
    with db.writing(engine) as session:
        v2 = add_version(session, 2).id
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v2, now=NOW)
        approved = add_workflow(session, v2, "sha256:a").id
        waiting = add_workflow(session, v2, "sha256:b").id
        repo.approve_workflow_version(session, approved, now=NOW)

    with db.writing(engine) as session:
        replaced = repo.approve_study_version(session, v3, now=NOW)
        assert replaced.id == v2

    with db.reading(engine) as session:
        assert repo.get_study_version(session, v2).state == "superseded"
        assert repo.get_study_version(session, v3).state == "approved"
        assert repo.approved_version(session, "st-01").id == v3
        for workflow in (approved, waiting):
            assert repo.get_workflow_version(session, workflow).state == "superseded"


def test_a_failed_approval_leaves_the_old_version_approved(engine):
    """The same transaction: nothing moves unless everything does."""
    with db.writing(engine) as session:
        v2 = add_version(session, 2).id
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v2, now=NOW)

    with pytest.raises(RuntimeError):
        with db.writing(engine) as session:
            repo.approve_study_version(session, v3, now=NOW)
            raise RuntimeError("the decision could not be recorded")

    assert state_of(engine, v2) == "approved"
    assert state_of(engine, v3) == "pending"


def test_while_v3_waits_v2_stays_approved(engine):
    with db.writing(engine) as session:
        v2 = add_version(session, 2).id
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v2, now=NOW)
        repo.refuse_study_version(session, v3, now=NOW)
    assert state_of(engine, v2) == "approved"
    assert state_of(engine, v3) == "refused"


def test_the_database_itself_refuses_two_approved_versions(engine):
    """A race, or any writer that skips the repo, still cannot break the rule."""
    with db.writing(engine) as session:
        for number in (1, 2):
            add_version(session, number)
    with pytest.raises(IntegrityError):
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "UPDATE study_version SET state = 'approved'")


def test_versions_received_out_of_order_still_swap(engine):
    """A poll can deliver v3 before v2, so the newer version has the lower key.

    A flush orders updates by key, which would approve v3 while v2 is still
    approved and trip the index, unless v2 moves first.
    """
    with db.writing(engine) as session:
        v3 = add_version(session, 3).id
        v2 = add_version(session, 2).id
        assert v3 < v2
        repo.approve_study_version(session, v2, now=NOW)
    with db.writing(engine) as session:
        repo.approve_study_version(session, v3, now=NOW)
    assert state_of(engine, v2) == "superseded"
    assert state_of(engine, v3) == "approved"


def test_versions_of_different_studies_are_independent(engine):
    with db.writing(engine) as session:
        one = add_version(session, 1, study="st-01").id
        two = add_version(session, 1, study="st-02").id
        repo.approve_study_version(session, one, now=NOW)
        repo.approve_study_version(session, two, now=NOW)
    assert state_of(engine, one) == state_of(engine, two) == "approved"


def test_an_older_version_cannot_replace_a_newer_one(engine):
    with db.writing(engine) as session:
        v2 = add_version(session, 2).id
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v3, now=NOW)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.approve_study_version(session, v2, now=NOW)


def test_concurrent_approvals_leave_exactly_one_approved(engine):
    with db.writing(engine) as session:
        ids = [add_version(session, number).id for number in range(1, 9)]

    failures = []

    def approve(version_id):
        try:
            with db.writing(engine) as session:
                repo.approve_study_version(session, version_id, now=NOW)
        except (Conflict, IllegalTransition):
            pass                         # lost to a newer version, as it should
        except Exception as failure:     # noqa: BLE001
            failures.append(failure)

    threads = [threading.Thread(target=approve, args=(i,)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    with db.reading(engine) as session:
        states = [repo.get_study_version(session, i).state for i in ids]
    assert states.count("approved") == 1


# Identity


def test_a_member_who_failed_verification_blocks_approval(engine):
    with db.writing(engine) as session:
        version = add_version(session, members=("m-01", "m-02")).id
        repo.set_identity(session, version, "m-02", False, "keys do not match")
    with pytest.raises(IdentityNotVerified, match="m-02"):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version, now=NOW)
    assert state_of(engine, version) == "pending"


def test_a_failed_member_can_still_be_refused(engine):
    with db.writing(engine) as session:
        version = add_version(session, verified=False).id
        repo.refuse_study_version(session, version, now=NOW)
    assert state_of(engine, version) == "refused"


def test_a_listed_member_with_no_key_row_blocks_approval(engine):
    """Absence must not read as verified."""
    with db.writing(engine) as session:
        row = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:x", raw=b"x",
            goal="g", proposer="m", members=[{"member": "ghost"}], nodes=[],
            datasets=[], output_rules={}, now=NOW)
        version = row.id
    with pytest.raises(IdentityNotVerified):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version, now=NOW)


def test_a_version_with_no_members_cannot_be_approved(engine):
    with db.writing(engine) as session:
        version = repo.add_study_version(
            session, study_id="st", version=1, digest="sha256:x", raw=b"x",
            goal="g", proposer="m", members=[], nodes=[], datasets=[],
            output_rules={}, now=NOW).id
    with pytest.raises(IdentityNotVerified):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version, now=NOW)


# State machines


@pytest.mark.parametrize("first,then", [
    (repo.refuse_study_version, repo.withdraw_study_version),
    (repo.refuse_study_version, repo.refuse_study_version),
])
def test_a_refused_version_stays_refused(engine, first, then):
    with db.writing(engine) as session:
        version = add_version(session).id
        first(session, version, now=NOW)
    with pytest.raises(IllegalTransition):
        with db.writing(engine) as session:
            then(session, version, now=NOW)


def test_a_withdrawn_version_cannot_be_approved_again(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.approve_study_version(session, version, now=NOW)
        repo.withdraw_study_version(session, version, now=NOW)
    with pytest.raises(IllegalTransition):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version, now=NOW)


def test_an_unknown_state_is_refused_by_the_database(engine):
    with db.writing(engine) as session:
        add_version(session)
    with pytest.raises(IntegrityError):
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "UPDATE study_version SET state = 'maybe'")


def test_workflows_need_an_approved_study_version(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        workflow = add_workflow(session, version).id
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.approve_workflow_version(session, workflow, now=NOW)


def test_a_workflow_can_be_withdrawn_alone(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.approve_study_version(session, version, now=NOW)
        kept = add_workflow(session, version, "sha256:a").id
        gone = add_workflow(session, version, "sha256:b").id
        repo.approve_workflow_version(session, kept, now=NOW)
        repo.approve_workflow_version(session, gone, now=NOW)
        repo.withdraw_workflow_version(session, gone, now=NOW)
        assert repo.approved_workflow(session, version, "sha256:a").id == kept
        assert repo.approved_workflow(session, version, "sha256:b") is None
        assert repo.get_study_version(session, version).state == "approved"


def test_parameter_variants_are_separate_workflows(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        add_workflow(session, version, "sha256:maf01")
        add_workflow(session, version, "sha256:maf05")
        assert len(repo.workflow_versions(session, version)) == 2


def test_a_workflow_approval_belongs_to_one_study_version(engine):
    """Approvals do not carry over, so the same digest is two rows."""
    with db.writing(engine) as session:
        v1 = add_version(session, 1).id
        v2 = add_version(session, 2).id
        repo.approve_study_version(session, v1, now=NOW)
        workflow = add_workflow(session, v1, "sha256:same").id
        repo.approve_workflow_version(session, workflow, now=NOW)
        repo.approve_study_version(session, v2, now=NOW)
        add_workflow(session, v2, "sha256:same")
        assert repo.approved_workflow(session, v2, "sha256:same") is None


def test_pending_lists_are_newest_first(engine):
    with db.writing(engine) as session:
        old = repo.add_study_version(
            session, study_id="a", version=1, digest="sha256:a", raw=b"a", goal="g",
            proposer="m", members=[], nodes=[], datasets=[], output_rules={},
            now=NOW - timedelta(days=1))
        new = repo.add_study_version(
            session, study_id="b", version=1, digest="sha256:b", raw=b"b", goal="g",
            proposer="m", members=[], nodes=[], datasets=[], output_rules={}, now=NOW)
        assert [row.id for row in repo.pending_study_versions(session)] == [new.id, old.id]
        workflow = add_workflow(session, new.id)
        assert [row.id for row in repo.pending_workflow_versions(session)] == [workflow.id]


# Decisions


def test_a_decision_cannot_be_rewritten(engine):
    with db.writing(engine) as session:
        decision = add_decision(session).id
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            session.get(Decision, decision).user = "someone else"


def test_every_subject_and_action_of_the_spec_is_accepted(engine):
    with db.writing(engine) as session:
        for subject in SubjectType:
            for action in Action:
                repo.record_decision(
                    session, subject_type=subject, subject_id="1", action=action,
                    user="u", object_raw=b"{}", signature="s", at=NOW)
        assert len(repo.decisions_about(session, SubjectType.RUN, "1")) == len(Action)


def test_flags_of_a_decision_are_kept(engine):
    with db.writing(engine) as session:
        repo.record_decision(
            session, subject_type=SubjectType.STUDY_VERSION, subject_id="1",
            action=Action.APPROVE, user="u", object_raw=b"{}", signature="s",
            at=NOW, dac_confirmed=True, reason="superseded", subject_hash="sha256:h",
            comment="ok")
    with db.reading(engine) as session:
        row = repo.decisions_about(session, SubjectType.STUDY_VERSION, "1")[0]
    assert (row.dac_confirmed, row.reason, row.subject_hash, row.comment) == (
        True, "superseded", "sha256:h", "ok")


def test_an_unposted_decision_can_be_undone(engine):
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.delete_unposted_decision(session, decision)
        assert repo.unposted_decisions(session) == []


def test_a_posted_decision_cannot_be_undone(engine):
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.mark_posted(session, decision, now=NOW)
    with pytest.raises(AlreadyPosted):
        with db.writing(engine) as session:
            repo.delete_unposted_decision(session, decision)
    with db.reading(engine) as session:
        assert session.get(Decision, decision) is not None


def test_undoing_a_decision_that_never_existed_says_so(engine):
    with pytest.raises(NotFound):
        with db.writing(engine) as session:
            repo.delete_unposted_decision(session, 404)


def test_a_decision_is_posted_once(engine):
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.mark_posted(session, decision, now=NOW)
        assert repo.unposted_decisions(session) == []
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.mark_posted(session, decision, now=NOW)


# Requests


def test_every_refusal_code_can_be_recorded(engine):
    assert len(RefusalCode) == 10
    with db.writing(engine) as session:
        for index, code in enumerate(RefusalCode):
            add_request(session, f"req_{index}")
            repo.set_verdict(session, f"req_{index}", Verdict.REFUSED,
                             failed_check=code.value)


def test_a_request_naming_an_unknown_study_version_is_still_recorded(engine):
    with db.writing(engine) as session:
        add_request(session, study="never-seen")
        repo.set_verdict(session, "req_1", Verdict.REFUSED,
                         failed_check=RefusalCode.BAD_SIGNATURE)
    with db.reading(engine) as session:
        assert repo.get_request(session, "req_1").failed_check == "bad_signature"


@pytest.mark.parametrize("verdict,kwargs", [
    (Verdict.REFUSED, {}),
    (Verdict.ACCEPTED, {"failed_check": "expired"}),
    (Verdict.JOINED, {}),
    (Verdict.ACCEPTED, {"joined_request_id": "req_0"}),
])
def test_verdicts_that_make_no_sense_are_refused(engine, verdict, kwargs):
    with db.writing(engine) as session:
        add_request(session)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.set_verdict(session, "req_1", verdict, **kwargs)


def test_a_code_outside_the_list_is_refused(engine):
    with db.writing(engine) as session:
        add_request(session)
    with pytest.raises(ValueError):
        with db.writing(engine) as session:
            repo.set_verdict(session, "req_1", Verdict.REFUSED, failed_check="nope")


def test_a_verdict_is_final(engine):
    with db.writing(engine) as session:
        add_request(session)
        repo.set_verdict(session, "req_1", Verdict.ACCEPTED, run_id="run_1")
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.set_verdict(session, "req_1", Verdict.REFUSED,
                             failed_check="expired")


def test_a_repeat_finds_the_request_it_repeats(engine):
    with db.writing(engine) as session:
        add_request(session, "req_1", payload="sha256:same")
        repo.set_verdict(session, "req_1", Verdict.ACCEPTED, run_id="run_1")
        add_request(session, "req_2", payload="sha256:same")
        found = repo.first_with_payload(session, "sha256:same", excluding="req_2")
        assert found.request_id == "req_1"
        repo.set_verdict(session, "req_2", Verdict.JOINED,
                         joined_request_id="req_1", run_id="run_1")


def test_a_request_not_yet_checked_is_not_a_repeat_target(engine):
    with db.writing(engine) as session:
        add_request(session, "req_1", payload="sha256:same")
        add_request(session, "req_2", payload="sha256:same")
        assert repo.first_with_payload(session, "sha256:same",
                                       excluding="req_2") is None


def test_the_same_request_id_with_other_bytes_is_refused(engine):
    with db.writing(engine) as session:
        add_request(session)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            add_request(session, raw=b"other")


def test_a_received_request_cannot_be_rewritten(engine):
    with db.writing(engine) as session:
        add_request(session)
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            session.get(Request, "req_1").workflow_digest = "sha256:other"


def test_recent_requests_are_filtered_and_newest_first(engine):
    with db.writing(engine) as session:
        add_request(session, "req_old", received=NOW - timedelta(days=2))
        add_request(session, "req_a", received=NOW - timedelta(hours=2))
        add_request(session, "req_b", received=NOW - timedelta(hours=1))
        repo.set_verdict(session, "req_a", Verdict.REFUSED, failed_check="expired")
        repo.set_verdict(session, "req_b", Verdict.ACCEPTED)
        since = NOW - timedelta(days=1)
        assert [r.request_id for r in repo.requests_since(session, since)] == [
            "req_b", "req_a"]
        assert [r.request_id for r in repo.requests_since(
            session, since, [Verdict.REFUSED])] == ["req_a"]


def test_a_study_pages_through_its_requests(engine):
    with db.writing(engine) as session:
        for index in range(25):
            add_request(session, f"req_{index:02d}",
                        received=NOW - timedelta(minutes=index))
        assert repo.count_requests_of_study(session, "st-01") == 25
        first = repo.requests_of_study(session, "st-01")
        second = repo.requests_of_study(session, "st-01", offset=20)
        assert [len(first), len(second)] == [20, 5]
        assert first[0].request_id == "req_00"


# Runs


def test_a_run_keeps_its_summary_and_step_times(engine):
    steps = [{"id": "qc", "state": "COMPLETE", "started_at": "a", "ended_at": "b"}]
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING",
                      workspace_path="/w", log_stdout="out")
        repo.save_run(session, "run_1", state="COMPLETE", steps=steps, exit_code=0,
                      node_logs={"qc": {"stderr": "e"}})
    with db.reading(engine) as session:
        run = repo.get_run(session, "run_1")
    assert (run.state, run.steps, run.exit_code, run.log_stdout,
            run.workspace_path) == ("COMPLETE", steps, 0, "out", "/w")
    assert run.node_logs == {"qc": {"stderr": "e"}}
    assert run.egress_state == "not_ready" and run.cancel_requested is False


def test_a_field_that_does_not_exist_is_refused(engine):
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.save_run(session, "run_1", state="QUEUED", nonsense=1)


@pytest.mark.parametrize("field", ["cancel_requested", "egress_state"])
def test_a_general_update_cannot_touch_cancel_or_egress(engine, field):
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.save_run(session, "run_1", state="QUEUED", **{field: "x"})


@pytest.mark.parametrize("state", ["QUEUED", "INITIALIZING", "RUNNING"])
def test_an_active_run_can_be_asked_to_cancel(engine, state):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state=state)
        repo.request_cancel(session, "run_1")
        assert [r.run_id for r in repo.runs_to_cancel(session)] == ["run_1"]


def test_a_cancel_that_was_acted_on_is_not_listed_again(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING")
        repo.request_cancel(session, "run_1")
        repo.save_run(session, "run_1", state="CANCELING")
        assert repo.runs_to_cancel(session) == []


@pytest.mark.parametrize("state", ["COMPLETE", "CANCELED", "SYSTEM_ERROR"])
def test_a_finished_run_cannot_be_cancelled(engine, state):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state=state)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.request_cancel(session, "run_1")


def walk_egress(session, run_id, *stages):
    for stage in stages:
        repo.set_egress_state(session, run_id, stage, now=NOW, retain_seconds=3600)


def test_egress_follows_both_modes(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "airlock", state="COMPLETE")
        repo.save_run(session, "manual", state="COMPLETE")
        walk_egress(session, "airlock", EgressState.RULES_PENDING,
                    EgressState.AWAITING_AIRLOCK, EgressState.RELEASED,
                    EgressState.SENT)
        walk_egress(session, "manual", EgressState.RULES_PENDING,
                    EgressState.AWAITING_REVIEW, EgressState.WITHHELD)
        assert repo.get_run(session, "airlock").egress_state == "sent"
        assert repo.get_run(session, "manual").egress_state == "withheld"


@pytest.mark.parametrize("stages", [
    (EgressState.SENT,),
    (EgressState.RULES_PENDING, EgressState.RELEASED),
    (EgressState.RULES_PENDING, EgressState.AWAITING_REVIEW,
     EgressState.WITHHELD, EgressState.RELEASED),
])
def test_egress_cannot_skip_a_stage(engine, stages):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="COMPLETE")
    with pytest.raises(IllegalTransition):
        with db.writing(engine) as session:
            walk_egress(session, "run_1", *stages)


def test_the_retention_clock_starts_only_when_egress_is_finished(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="COMPLETE")
        walk_egress(session, "run_1", EgressState.RULES_PENDING,
                    EgressState.AWAITING_AIRLOCK, EgressState.RELEASED)
        assert repo.get_run(session, "run_1").retain_until is None
        walk_egress(session, "run_1", EgressState.SENT)
        assert repo.get_run(session, "run_1").retain_until == NOW + timedelta(hours=1)


def test_a_run_waiting_for_egress_is_listed_however_old(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "waiting", state="COMPLETE",
                      ended_at=NOW - timedelta(days=30))
        walk_egress(session, "waiting", EgressState.RULES_PENDING,
                    EgressState.AWAITING_REVIEW)
        repo.save_run(session, "done", state="COMPLETE")
        walk_egress(session, "done", EgressState.RULES_PENDING,
                    EgressState.AWAITING_REVIEW, EgressState.WITHHELD)
        repo.save_run(session, "fresh", state="RUNNING")
        assert [r.run_id for r in repo.runs_awaiting_egress(session)] == ["waiting"]


def test_retention_can_be_rebuilt_from_the_database(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "kept", state="COMPLETE", workspace_path="/w")
        repo.save_run(session, "gone", state="COMPLETE")
    with db.reading(engine) as session:
        assert [r.workspace_path for r in repo.runs_with_workspaces(session)] == ["/w"]


def test_busy_slots_come_from_run_states(engine):
    with db.writing(engine) as session:
        for name, state in (("a", "RUNNING"), ("b", "INITIALIZING"),
                            ("c", "CANCELING"), ("d", "QUEUED"), ("e", "COMPLETE")):
            repo.save_run(session, name, state=state)
        assert repo.count_busy_runs(session) == 3


# Dataset bindings


def bind(session, source, location="a/", dataset="EGAD1"):
    return repo.bind_dataset(session, dataset_id=dataset, source=source,
                             type="localpath", location=location, bound_by="u",
                             now=NOW)


def test_the_page_cannot_override_the_config_file(engine):
    with db.writing(engine) as session:
        bind(session, BindingSource.CONFIG)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            bind(session, BindingSource.PAGE, "b/")
    with db.reading(engine) as session:
        assert repo.dataset_binding(session, "EGAD1").location == "a/"


def test_the_config_file_overrides_the_page(engine):
    with db.writing(engine) as session:
        bind(session, BindingSource.PAGE)
        bind(session, BindingSource.CONFIG, "b/")
        row = repo.dataset_binding(session, "EGAD1")
    assert (row.source, row.location) == ("config", "b/")


def test_the_page_can_edit_and_remove_its_own(engine):
    with db.writing(engine) as session:
        bind(session, BindingSource.PAGE)
        bind(session, BindingSource.PAGE, "b/")
        assert repo.dataset_binding(session, "EGAD1").location == "b/"
        repo.unbind_dataset(session, "EGAD1")
        assert repo.dataset_bindings(session) == []


def test_a_config_binding_cannot_be_removed(engine):
    with db.writing(engine) as session:
        bind(session, BindingSource.CONFIG)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.unbind_dataset(session, "EGAD1")


# Federation list and coordinator status


def store_list(session, version, raw=b"list"):
    return repo.store_federation_list(session, version=version, raw=raw,
                                      signature="sig", nodes=[], now=NOW)


def test_an_older_node_list_is_refused(engine):
    with db.writing(engine) as session:
        store_list(session, 14)
    with pytest.raises(StaleList):
        with db.writing(engine) as session:
            store_list(session, 13)
    with db.reading(engine) as session:
        assert repo.latest_federation_list(session).version == 14


def test_a_newer_list_replaces_and_the_same_one_is_harmless(engine):
    with db.writing(engine) as session:
        store_list(session, 14)
        store_list(session, 14)
        store_list(session, 15)
        assert repo.latest_federation_list(session).version == 15


def test_the_same_version_with_other_bytes_is_refused(engine):
    with db.writing(engine) as session:
        store_list(session, 14)
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            store_list(session, 14, b"other")


def test_a_failed_poll_keeps_the_last_success(engine):
    later = NOW + timedelta(minutes=1)
    with db.writing(engine) as session:
        assert repo.node_status(session) is None
        repo.record_poll(session, ok=True, now=NOW)
        repo.record_poll(session, ok=False, error="timeout", now=later)
        status = repo.node_status(session)
    assert (status.last_poll_at, status.last_success_at, status.last_error) == (
        later, NOW, "timeout")


def test_a_good_poll_clears_the_error(engine):
    with db.writing(engine) as session:
        repo.record_poll(session, ok=False, error="timeout", now=NOW)
        repo.record_poll(session, ok=True, now=NOW)
        assert repo.node_status(session).last_error is None


# Two processes


def test_a_second_process_sees_what_the_first_committed(tmp_path):
    url, engine = fresh_database(tmp_path)
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING")
    code = (
        "import sys\n"
        "from store import db, repo\n"
        "engine = db.make_engine(sys.argv[1])\n"
        "with db.writing(engine) as session:\n"
        "    repo.request_cancel(session, 'run_1')\n"
    )
    subprocess.run([sys.executable, "-c", code, url], cwd=REPO_ROOT, check=True)
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_1").cancel_requested is True


def test_the_database_is_in_wal_mode(engine):
    """Two processes open this file, and the page must not stall a writer."""
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_a_reader_is_not_blocked_by_an_open_write(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING")
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="COMPLETE")
        with db.reading(engine) as reader:
            assert repo.get_run(reader, "run_1").state == "RUNNING"


def test_many_writers_wait_rather_than_fail(engine):
    """Read then write, the shape that BEGIN IMMEDIATE exists for."""
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING", exit_code=0)

    failures = []

    def bump():
        try:
            for _ in range(20):
                with db.writing(engine) as session:
                    run = repo.get_run(session, "run_1")
                    session.refresh(run)
                    repo.save_run(session, "run_1", exit_code=run.exit_code + 1)
        except Exception as failure:     # noqa: BLE001
            failures.append(failure)

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_1").exit_code == 120


def test_foreign_keys_are_enforced(engine):
    """SQLite leaves them off unless asked, on every connection."""
    with pytest.raises(IntegrityError):
        with db.writing(engine) as session:
            session.add(MemberKey(study_version_id=999, member="m", ls_aai_id="a",
                                  signing_key={}, result_key="k",
                                  binding_token="t"))
            session.flush()


def test_versions_states_are_the_spec_ones():
    assert {state.value for state in VersionState} == {
        "pending", "approved", "refused", "withdrawn", "superseded"}
