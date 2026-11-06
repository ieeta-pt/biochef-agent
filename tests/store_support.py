"""A migrated database for the store's tests, and rows to put in it."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from store import db, migrate, repo  # noqa: E402
from store.states import Action, SubjectType  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
RULES = {"aggregates_only": True, "min_count": 10, "types": ["tsv"],
         "max_bytes": 1000}


def fresh_database(tmp_path):
    url = f"sqlite:///{tmp_path}/agent.db"
    migrate.upgrade(url)
    return url, db.make_engine(url)


def add_version(session, version=1, *, study="st-01", members=("m-01",),
                verified=True):
    entries = [{"member": name} for name in members]
    row = repo.add_study_version(
        session, study_id=study, version=version,
        digest=f"sha256:{study}-{version}", raw=f'{{"v":{version}}}'.encode(),
        goal="A study", proposer=members[0], members=entries, nodes=[],
        datasets=["EGAD1"], output_rules=RULES, now=NOW)
    for name in members:
        repo.add_member_key(session, study_version_id=row.id, member=name,
                            ls_aai_id=f"{name}@example.org",
                            signing_key={"kty": "EC"}, result_key="age1x",
                            binding_token="token")
    session.flush()
    for name in members:
        repo.set_identity(session, row.id, name, verified,
                          None if verified else "no match")
    return row


def add_workflow(session, study_version_id, digest="sha256:w1"):
    return repo.add_workflow_version(
        session, study_version_id=study_version_id, digest=digest, raw=b"{}",
        steps=[{"id": "qc", "repo": "tool", "digest": "sha256:t"}], now=NOW)


def add_request(session, request_id="req_1", *, payload="sha256:p", raw=b"{}",
                received=NOW, study="st-01"):
    return repo.add_request(
        session, request_id=request_id, study_id=study, study_version=1,
        study_digest="sha256:st", workflow_digest="sha256:w1", member="m-01",
        node="node-a", datasets=["EGAD1"], not_after=NOW + timedelta(days=1),
        payload_digest=payload, raw=raw, now=received)


def add_decision(session, subject_id="1", action=Action.APPROVE):
    return repo.record_decision(
        session, subject_type=SubjectType.STUDY_VERSION, subject_id=subject_id,
        action=action, user="j.silva", object_raw=b"{}", signature="sig", at=NOW)
