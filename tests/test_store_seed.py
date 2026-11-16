"""The seed reproduces the mockups, and only ever fills an empty database."""

import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlmodel import select

from tests.store_support import NOW, REPO_ROOT, fresh_database

from node import decisions
from node.keys import generate
from store import db, migrate, repo, seed
from store.errors import StoreError
from store.fixtures import mock
from store.models import Decision, MemberKey, Request, Study, StudyVersion
from store.states import SubjectType, Verdict


@pytest.fixture
def engine(tmp_path):
    engine = fresh_database(tmp_path)[1]
    seed.seed(engine, now=NOW)
    return engine


def test_the_screens_show_what_the_mockups_show(engine):
    with db.reading(engine) as session:
        cardio = {row.version: row.state for row in
                  repo.study_versions(session, mock.CARDIO)}
        assert cardio == {1: "superseded", 2: "approved", 3: "pending"}
        assert repo.study_versions(session, mock.MICROBIOME)[0].state == "withdrawn"
        assert repo.approved_version(session, mock.SEPSIS).version == 1

        pending = repo.pending_study_versions(session)
        workflows = repo.pending_workflow_versions(session)
        assert [repo.study_heading(session, row) for row in pending] == [
            "cardio-pt · CARDIO-PT cohort",
            "rare-variants-pilot · Rare variants pilot"]
        assert [(row.name, row.version) for row in workflows] == [("gwas-qc", "1.4.0")]


def test_the_failed_identity_blocks_rare_variants(engine):
    with db.reading(engine) as session:
        version = repo.study_versions(session, mock.RARE)[0]
        keys = {row.member: row for row in repo.member_keys(session, version.id)}
    marta, paulo = keys["m-04"], keys["m-05"]
    assert marta.identity_verified and marta.display_name == "Marta Reis"
    assert not paulo.identity_verified and paulo.identity_error
    assert paulo.display_name is None
    assert repo.member_label(paulo) == "c2e86f10…@lifescience-ri.eu"
    with pytest.raises(Exception, match="m-05"):
        with db.writing(engine) as session:
            repo.approve_study_version(session, version.id)


def test_every_seeded_name_is_the_claim_of_its_token(engine):
    with db.reading(engine) as session:
        rows = session.exec(select(MemberKey)).all()
    named = [row for row in rows if row.display_name]
    assert len(named) == len(rows) - 1
    for row in named:
        claims = seed._token_claims(row.binding_token)
        assert (claims["sub"], claims["name"]) == (row.ls_aai_id, row.display_name)


def test_names_appear_in_no_signed_document(engine):
    """A name comes from the token, so nothing else may carry one."""
    with db.reading(engine) as session:
        versions = session.exec(select(StudyVersion)).all()
        requests = session.exec(select(Request)).all()
    documents = [row.raw for row in [*versions, *requests]]
    assert documents
    for name in mock.MEMBERS:
        assert not any(name.encode() in raw for raw in documents)
    assert all(row.proposer.startswith("m-") for row in versions)
    assert all(row.member.startswith("m-") for row in requests)


def test_the_study_heading_is_the_id_and_the_first_line_of_the_goal(engine):
    with db.reading(engine) as session:
        version = repo.approved_version(session, mock.CARDIO)
        assert session.get(Study, mock.CARDIO).title is None
        assert repo.study_heading(session, version) == "cardio-pt · CARDIO-PT cohort"


def test_v2_replaced_v1_with_two_decisions(engine):
    with db.reading(engine) as session:
        v1, v2 = reversed(repo.study_versions(session, mock.CARDIO)[1:])
        about_v1 = repo.decisions_about(session, SubjectType.STUDY_VERSION, v1.id)
        about_v2 = repo.decisions_about(session, SubjectType.STUDY_VERSION, v2.id)
    assert about_v1[0].action == "withdraw"
    assert about_v1[0].reason == "superseded"
    assert about_v1[0].user == seed.SYSTEM_USER
    assert [row.action for row in about_v2] == ["approve"]
    assert about_v2[0].dac_confirmed is True


def test_the_workflows_of_v2_are_the_two_variants_and_the_new_version(engine):
    with db.reading(engine) as session:
        version = repo.approved_version(session, mock.CARDIO)
        rows = repo.workflow_versions(session, version.id)
    short = {row.digest[7:11] + row.digest[-4:]: row.state for row in rows}
    assert short == {"2a77c5e8": "approved", "6d02a1f3": "approved",
                     "f09d1b44": "approved", "4be190c3": "pending"}


def test_the_last_day_is_mock_04(engine):
    since = NOW - timedelta(hours=24)
    with db.reading(engine) as session:
        rows = repo.requests_since(session, since)
        refused = repo.requests_since(session, since, [Verdict.REFUSED])
    assert [row.request_id for row in rows] == [
        "req_8e90", "req_8f40", "req_8f33", "req_8f22", "req_8f21", "req_7c03"]
    assert {row.request_id for row in refused} == {"req_8f40", "req_8f33"}


def test_the_study_counts_are_mock_02(engine):
    with db.reading(engine) as session:
        assert repo.count_requests_of_study(session, mock.CARDIO) == 14
        assert repo.count_requests_of_study(session, mock.SEPSIS) == 6
        for request_id in ("req_7b88", "req_7a10"):
            assert repo.get_request(session, request_id).study_id == mock.CARDIO


def test_the_joined_request_points_at_its_run(engine):
    with db.reading(engine) as session:
        joined = repo.get_request(session, "req_8f22")
        original = repo.get_request(session, "req_8f21")
    assert joined.verdict == "joined" and joined.joined_request_id == "req_8f21"
    assert joined.run_id == original.run_id == "run_8f21"
    assert joined.payload_digest == original.payload_digest


def test_a_refused_request_has_its_signed_refusal(engine):
    with db.reading(engine) as session:
        decisions = repo.decisions_about(session, SubjectType.REQUEST, "req_8f40")
    assert [(row.action, row.reason) for row in decisions] == [
        ("refuse", "workflow_not_approved")]


def test_the_run_of_mock_05(engine):
    with db.reading(engine) as session:
        run = repo.get_run(session, "run_8f21")
        assert repo.count_busy_runs(session) == 1
        assert run in repo.runs_awaiting_egress(session)
    assert run.state == "COMPLETE" and run.egress_state == "awaiting_airlock"
    assert [row["file"] for row in run.egress_files] == [
        "assoc_summary.tsv", "manhattan.png", "qc_log.txt"]
    assert [row["id"] for row in run.steps] == list(mock.STEP_MINUTES)


def test_the_bindings_of_mock_06(engine):
    with db.reading(engine) as session:
        rows = repo.dataset_bindings(session)
    assert {(row.dataset_id, row.source) for row in rows} == {
        (mock.CARDIO_DATASET, "config"), (mock.SEPSIS_DATASET, "config"),
        (mock.MICROBIOME_DATASET, "page")}
    assert mock.RARE_DATASET not in {row.dataset_id for row in rows}


def test_the_node_of_mock_07(engine):
    with db.reading(engine) as session:
        latest = repo.latest_federation_list(session)
        assert repo.node_status(session).last_success_at == NOW
    assert latest.version == 14 and len(latest.nodes) == 5


def test_every_seeded_decision_verifies_under_the_seeds_own_key(tmp_path):
    engine = fresh_database(tmp_path)[1]
    keys = generate()
    seed.seed(engine, now=NOW, keys=keys)
    with db.reading(engine) as session:
        rows = session.exec(select(Decision)).all()
    assert rows
    for row in rows:
        document = decisions.verify(row.object_raw, row.signature,
                                    keys.signing.public_key())
        assert (document["action"], document["user"]) == (row.action, row.user)


def test_a_seed_key_is_thrown_away(engine):
    """Nothing in the database can sign for a node."""
    with db.reading(engine) as session:
        row = session.exec(select(Decision)).first()
    with pytest.raises(decisions.DecisionError):
        decisions.verify(row.object_raw, row.signature,
                         generate().signing.public_key())


def test_the_seed_writes_nothing_to_the_audit_trail(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(trail))
    seed.seed(fresh_database(tmp_path)[1], now=NOW)
    assert not trail.exists()


def test_no_private_key_is_stored(engine):
    with db.reading(engine) as session:
        version = repo.approved_version(session, mock.CARDIO)
        keys = repo.member_keys(session, version.id)
    assert keys and all("d" not in row.signing_key for row in keys)


def test_seeded_times_follow_the_clock(tmp_path):
    """Seeded later, the 24 hour view is still full."""
    engine = fresh_database(tmp_path)[1]
    later = NOW + timedelta(days=365)
    seed.seed(engine, now=later)
    with db.reading(engine) as session:
        assert len(repo.requests_since(session, later - timedelta(hours=24))) == 6


def test_a_database_with_data_is_refused(engine):
    with pytest.raises(StoreError, match="already holds data"):
        seed.seed(engine, now=NOW)


def test_a_database_behind_the_code_is_refused(tmp_path):
    url = f"sqlite:///{tmp_path}/agent.db"
    engine = db.make_engine(url)
    with pytest.raises(StoreError, match="python -m store migrate"):
        seed.seed(engine)
    migrate.upgrade(url)
    seed.seed(engine)


def test_the_command_line_seeds_and_refuses_a_second_time(tmp_path):
    url = fresh_database(tmp_path)[0]
    env = {**os.environ, "BIOCHEF_DB_URL": url}
    command = [sys.executable, "-m", "store.seed", "--fixture", "mock"]
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)
    second = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True,
                            text=True)
    assert second.returncode == 1
    assert "already holds data" in second.stderr
