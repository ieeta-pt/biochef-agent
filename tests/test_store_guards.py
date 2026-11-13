"""What the store refuses even from a caller that catches and carries on
(agent UI, part 1).

Each rule here is one a later caller could break without noticing: approving
twice, rewriting what is under review, a decision undone while its POST is on
the way, a name that reorders the page around it.
"""

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from tests.store_support import (NOW, REPO_ROOT, add_decision, add_request,
                                 add_version, add_workflow, fresh_database)

import runs
from store import db, repo
from store.errors import (AlreadyPosted, Conflict, IllegalTransition,
                          ImmutableField, NotFound, StoreError)
from store.models import Decision, MemberKey, RunSummary
from store.states import RUN_STATES, EgressState, VersionState


@pytest.fixture
def engine(tmp_path):
    return fresh_database(tmp_path)[1]


def state_of(engine, version_id):
    with db.reading(engine) as session:
        return repo.get_study_version(session, version_id).state


def workflow_state(engine, workflow_id):
    with db.reading(engine) as session:
        return repo.get_workflow_version(session, workflow_id).state


# Approving checks before it moves anything


def test_approving_an_approved_version_again_moves_nothing(engine):
    """A double click, from a caller that catches the error and commits."""
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.approve_study_version(session, version, now=NOW)
        workflow = add_workflow(session, version).id
        repo.approve_workflow_version(session, workflow, now=NOW)

    with db.writing(engine) as session:
        with pytest.raises(IllegalTransition):
            repo.approve_study_version(session, version, now=NOW)

    assert state_of(engine, version) == "approved"
    assert workflow_state(engine, workflow) == "approved"


@pytest.mark.parametrize("end", [repo.refuse_study_version,
                                 repo.withdraw_study_version])
def test_approving_a_closed_version_leaves_the_approved_one(engine, end):
    with db.writing(engine) as session:
        v2 = add_version(session, 2).id
        if end is repo.withdraw_study_version:
            repo.approve_study_version(session, v2, now=NOW)
        end(session, v2, now=NOW)
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v3, now=NOW)
        workflow = add_workflow(session, v3).id
        repo.approve_workflow_version(session, workflow, now=NOW)

    with db.writing(engine) as session:
        with pytest.raises(IllegalTransition):
            repo.approve_study_version(session, v2, now=NOW)

    assert state_of(engine, v3) == "approved"
    assert workflow_state(engine, workflow) == "approved"


# Nothing stays pending for ever


def test_approving_a_newer_version_supersedes_the_older_ones_waiting(engine):
    with db.writing(engine) as session:
        v1 = add_version(session, 1).id
        v2 = add_version(session, 2).id
        v3 = add_version(session, 3).id
        v4 = add_version(session, 4).id
        waiting = add_workflow(session, v2).id
        repo.refuse_study_version(session, v1, now=NOW)
        repo.approve_study_version(session, v3, now=NOW)

    assert state_of(engine, v1) == "refused"
    assert state_of(engine, v2) == "superseded"
    assert workflow_state(engine, waiting) == "superseded"
    assert state_of(engine, v3) == "approved"
    assert state_of(engine, v4) == "pending"
    with db.reading(engine) as session:
        assert [row.id for row in repo.pending_study_versions(session)] == [v4]


def test_a_version_older_than_the_approved_one_arrives_superseded(engine):
    """Polling can deliver v2 after v3 was approved."""
    with db.writing(engine) as session:
        v3 = add_version(session, 3).id
        repo.approve_study_version(session, v3, now=NOW)
        v2 = add_version(session, 2).id
        v4 = add_version(session, 4).id
    assert state_of(engine, v2) == "superseded"
    assert state_of(engine, v4) == "pending"
    with db.reading(engine) as session:
        assert [row.id for row in repo.pending_study_versions(session)] == [v4]


def test_withdrawing_a_version_withdraws_its_open_workflows(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.approve_study_version(session, version, now=NOW)
        approved = add_workflow(session, version, "sha256:a").id
        waiting = add_workflow(session, version, "sha256:b").id
        refused = add_workflow(session, version, "sha256:c").id
        repo.approve_workflow_version(session, approved, now=NOW)
        repo.refuse_workflow_version(session, refused, now=NOW)
        repo.withdraw_study_version(session, version, now=NOW + timedelta(hours=1))

    assert workflow_state(engine, approved) == "withdrawn"
    assert workflow_state(engine, waiting) == "withdrawn"
    assert workflow_state(engine, refused) == "refused"
    with db.reading(engine) as session:
        assert repo.approved_workflow(session, version, "sha256:a") is None


# What is under review stays what it was


def add_run(session, run_id="run_1", files=None):
    return repo.save_run(session, run_id, state="COMPLETE",
                         egress_files=files or [{"file": "a.tsv", "size": 1}])


def to_stage(session, run_id, *stages):
    for stage in stages:
        repo.set_egress_state(session, run_id, stage, now=NOW, retain_seconds=60)


AFTER_RULES = [
    (EgressState.RULES_PENDING, EgressState.AWAITING_REVIEW),
    (EgressState.RULES_PENDING, EgressState.AWAITING_AIRLOCK),
    (EgressState.RULES_PENDING, EgressState.AWAITING_REVIEW, EgressState.RELEASED),
    (EgressState.RULES_PENDING, EgressState.AWAITING_REVIEW, EgressState.WITHHELD),
]


@pytest.mark.parametrize("stages", AFTER_RULES)
@pytest.mark.parametrize("field,value", [
    ("egress_files", [{"file": "secret.bam", "size": 9, "verdict": "pass"}]),
    ("airlock_ticket", "airlock-9999"),
    ("airlock_at", NOW + timedelta(days=1)),
])
def test_what_is_under_review_cannot_change(engine, stages, field, value):
    with db.writing(engine) as session:
        add_run(session)
        to_stage(session, "run_1", *stages)
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            repo.save_run(session, "run_1", **{field: value})
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            setattr(session.get(RunSummary, "run_1"), field, value)
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_1").egress_files == [
            {"file": "a.tsv", "size": 1}]


def test_the_rules_can_still_write_what_they_judged(engine):
    """egress.start writes the verdicts and the ticket at rules_pending."""
    judged = [{"file": "a.tsv", "size": 1, "verdict": "pass"}]
    with db.writing(engine) as session:
        add_run(session)
        to_stage(session, "run_1", EgressState.RULES_PENDING)
        repo.save_run(session, "run_1", egress_files=judged,
                      airlock_ticket="airlock-1", airlock_at=NOW)
        to_stage(session, "run_1", EgressState.AWAITING_AIRLOCK)
    with db.reading(engine) as session:
        run = repo.get_run(session, "run_1")
    assert run.egress_files == judged and run.airlock_ticket == "airlock-1"


def test_files_cannot_change_in_the_flush_that_closes_the_rules(engine):
    with db.writing(engine) as session:
        add_run(session)
        to_stage(session, "run_1", EgressState.RULES_PENDING)
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            row = session.get(RunSummary, "run_1")
            row.egress_files = [{"file": "other", "size": 2}]
            row.egress_state = EgressState.AWAITING_REVIEW.value


def test_the_retention_clock_is_not_set_through_save_run(engine):
    with db.writing(engine) as session:
        add_run(session)
    with pytest.raises(Conflict, match="retain_until"):
        with db.writing(engine) as session:
            repo.save_run(session, "run_1", retain_until=NOW)


@pytest.mark.parametrize("last", [EgressState.SENT, EgressState.WITHHELD])
def test_finishing_egress_needs_a_retention(engine, last):
    path = {EgressState.SENT: (EgressState.RULES_PENDING,
                               EgressState.AWAITING_REVIEW, EgressState.RELEASED),
            EgressState.WITHHELD: (EgressState.RULES_PENDING,
                                   EgressState.AWAITING_REVIEW)}[last]
    with db.writing(engine) as session:
        add_run(session)
        to_stage(session, "run_1", *path)
    with pytest.raises(Conflict, match="retain_seconds"):
        with db.writing(engine) as session:
            repo.set_egress_state(session, "run_1", last, now=NOW)
    with db.reading(engine) as session:
        run = repo.get_run(session, "run_1")
    assert run.egress_state == path[-1].value and run.retain_until is None


# save_run


def test_save_run_cannot_change_the_key(engine):
    """run_id is the positional argument, so Python refuses it as a field."""
    with db.writing(engine) as session:
        add_run(session)
    with pytest.raises(TypeError):
        with db.writing(engine) as session:
            repo.save_run(session, "run_1", **{"run_id": "run_2"})
    with db.reading(engine) as session:
        assert session.get(RunSummary, "run_1") is not None
        assert session.get(RunSummary, "run_2") is None


def test_save_run_checks_every_name_before_setting_any(engine):
    with db.writing(engine) as session:
        add_run(session)
        with pytest.raises(Conflict, match="no field"):
            repo.save_run(session, "run_1", exit_code=3, no_such_field=1)
        assert session.get(RunSummary, "run_1").exit_code is None


def test_a_new_run_without_a_state_is_a_conflict(engine):
    with pytest.raises(Conflict, match="needs a state"):
        with db.writing(engine) as session:
            repo.save_run(session, "run_9", exit_code=0)


# Member keys


KEY_FIELDS = [("ls_aai_id", "someone@else.org"), ("signing_key", {"kty": "OKP"}),
              ("result_key", "age1other"), ("binding_token", "other-token"),
              ("member", "m-99"), ("study_version_id", 99)]


@pytest.mark.parametrize("field,value", KEY_FIELDS)
def test_a_member_key_cannot_be_rewritten(engine, field, value):
    """They come from the signed study version, and result_key decides who a
    result is encrypted for."""
    with db.writing(engine) as session:
        version = add_version(session).id
    with pytest.raises(ImmutableField):
        with db.writing(engine) as session:
            setattr(session.get(MemberKey, (version, "m-01")), field, value)


def test_the_outcome_of_the_identity_check_can_still_change(engine):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.mark_identity_failed(session, version, "m-01", "key replaced")
        repo.mark_identity_verified(session, version, "m-01",
                                    {"sub": "m-01@example.org", "name": "Ana"})
        row = session.get(MemberKey, (version, "m-01"))
        assert (row.identity_verified, row.identity_error, row.display_name) == (
            True, None, "Ana")


def key_args(version, **changes):
    args = {"study_version_id": version, "member": "m-01",
            "ls_aai_id": "m-01@example.org", "signing_key": {"kty": "EC"},
            "result_key": "age1x", "binding_token": "token"}
    return {**args, **changes}


def test_adding_the_same_keys_again_is_harmless(engine):
    """A repeated poll adds them again."""
    with db.writing(engine) as session:
        version = add_version(session).id
        again = repo.add_member_key(session, **key_args(version))
        assert again is session.get(MemberKey, (version, "m-01"))
        assert again.identity_verified


@pytest.mark.parametrize("field,value", KEY_FIELDS[:4])
def test_other_keys_for_the_same_member_are_refused(engine, field, value):
    with db.writing(engine) as session:
        version = add_version(session).id
    with pytest.raises(Conflict):
        with db.writing(engine) as session:
            repo.add_member_key(session, **key_args(version, **{field: value}))


# Names


@pytest.mark.parametrize("name", [
    "Ana‮acoC",          # right-to-left override
    "Ana⁦Costa⁩",   # isolates
    "An‍a",              # zero-width joiner
    "Ana​Costa",         # zero-width space
    "Ana﻿Costa",         # byte order mark
    "Ana\x7fCosta",           # DEL
    "Ana\x85Costa",           # next line, a C1 control
    "Ana Costa",         # line separator
    "Ana Costa",         # paragraph separator
    "x" * (repo.MAX_NAME_LENGTH + 1),
])
def test_a_name_that_could_disguise_the_page_is_no_name(engine, name):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.mark_identity_verified(session, version, "m-01",
                                    {"sub": "m-01@example.org", "name": name})
        row = session.get(MemberKey, (version, "m-01"))
        assert row.display_name is None
        assert row.identity_verified
        assert repo.member_label(row) == "m-01@example.org"


@pytest.mark.parametrize("name", ["João Andrade", "Zoë Ångström", "李小龙",
                                  "Nguyễn Văn An", "x" * repo.MAX_NAME_LENGTH])
def test_ordinary_names_are_kept(engine, name):
    with db.writing(engine) as session:
        version = add_version(session).id
        repo.mark_identity_verified(session, version, "m-01",
                                    {"sub": "m-01@example.org", "name": name})
        assert session.get(MemberKey, (version, "m-01")).display_name == name


# Posting decisions


def test_a_posted_decision_stays_posted(engine):
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.mark_posted(session, decision, now=NOW)
    for field in ("posted_to_coordinator_at", "posting_started_at"):
        for value in (None, NOW + timedelta(hours=1)):
            with pytest.raises(ImmutableField):
                with db.writing(engine) as session:
                    setattr(session.get(Decision, decision), field, value)
    with db.reading(engine) as session:
        row = session.get(Decision, decision)
    assert row.posted_to_coordinator_at == NOW and repo.has_left(row)


def test_a_claimed_decision_cannot_be_undone(engine):
    """The poll claims, commits, then POSTs. An undo between the two is refused,
    so the coordinator never holds a decision this node deleted."""
    with db.writing(engine) as session:
        decision = add_decision(session).id
    with db.writing(engine) as session:
        claimed = repo.claim_for_posting(session, decision, now=NOW)
        assert claimed.posting_started_at == NOW and repo.has_left(claimed)
    with pytest.raises(AlreadyPosted):
        with db.writing(engine) as session:
            repo.delete_unposted_decision(session, decision)
    with db.writing(engine) as session:
        repo.mark_posted(session, decision, now=NOW)
        assert repo.unposted_decisions(session) == []


def test_an_undone_decision_cannot_be_claimed(engine):
    """The undo got there first: the poll learns it and sends nothing."""
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.delete_unposted_decision(session, decision)
    with pytest.raises(NotFound):
        with db.writing(engine) as session:
            repo.claim_for_posting(session, decision, now=NOW)


def test_a_claim_that_never_finished_can_be_retried(engine):
    """The process stopped between the claim and mark_posted."""
    with db.writing(engine) as session:
        decision = add_decision(session).id
        repo.claim_for_posting(session, decision, now=NOW)
    with db.writing(engine) as session:
        again = repo.claim_for_posting(session, decision,
                                       now=NOW + timedelta(minutes=5))
        assert again.posting_started_at == NOW
        assert [row.id for row in repo.unposted_decisions(session)] == [decision]


def test_an_unclaimed_decision_has_not_left(engine):
    with db.writing(engine) as session:
        assert not repo.has_left(add_decision(session))


def test_undo_and_claim_racing_never_both_win(engine):
    """Two writers on one file: either the undo wins and nothing is sent, or the
    claim wins and the decision stays. Never a sent decision that is gone."""
    for attempt in range(25):
        with db.writing(engine) as session:
            decision = add_decision(session, subject_id=str(attempt)).id
        outcome = {}
        start = threading.Barrier(2)

        def undo():
            start.wait()
            try:
                with db.writing(engine) as session:
                    repo.delete_unposted_decision(session, decision)
                outcome["undo"] = True
            except AlreadyPosted:
                outcome["undo"] = False

        def claim():
            start.wait()
            try:
                with db.writing(engine) as session:
                    repo.claim_for_posting(session, decision, now=NOW)
                outcome["claim"] = True
            except NotFound:
                outcome["claim"] = False

        threads = [threading.Thread(target=undo), threading.Thread(target=claim)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcome["undo"] != outcome["claim"], outcome
        with db.reading(engine) as session:
            kept = session.get(Decision, decision)
        assert (kept is not None) == outcome["claim"]


# Schema


def test_the_run_states_are_the_runners(engine):
    assert RUN_STATES == {state.value for state in runs.RunState}


def test_an_unknown_run_state_is_refused_by_the_database(engine):
    with db.writing(engine) as session:
        add_run(session)
    with pytest.raises(IntegrityError):
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "UPDATE run_summary SET state = 'FINISHED'")


@pytest.mark.parametrize("assignment", [
    "verdict = 'refused'",
    "failed_check = 'expired'",
    "verdict = 'accepted', failed_check = 'expired'",
    "verdict = 'joined'",
])
def test_a_verdict_and_its_reason_go_together_in_the_database(engine, assignment):
    with db.writing(engine) as session:
        add_request(session)
        add_request(session, "req_2", payload="sha256:q")
    with pytest.raises(IntegrityError):
        with engine.connect() as connection:
            connection.exec_driver_sql(
                f"UPDATE request SET {assignment} WHERE request_id = 'req_2'")


def test_only_sqlite_is_accepted():
    with pytest.raises(StoreError, match="only SQLite"):
        db.make_engine("postgresql://u:p@localhost/agent")


def test_the_first_migration_does_not_import_the_models():
    """So a later change to models.py cannot break a migration already run."""
    source = (Path(REPO_ROOT) / "store/migrations/versions/"
              "0001_agent_ui_tables.py").read_text()
    assert "store.models" not in source and "store.types.UtcDateTime" in source
