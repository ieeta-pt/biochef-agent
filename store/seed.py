"""python -m store.seed --fixture mock: fill a database with invented data.

For development and for reproducing the mockups, never for a real node. It
refuses a database that already holds anything, so that a mistyped
BIOCHEF_DB_URL cannot mix invented approvals into a node's record.

Decisions carry the signature `unsigned-fixture` until node signing exists.
Nothing could verify them as a real signature, which is the point.

Display names come out of the fixture tokens, and tokens are not really signed
either. Member ids stay opaque everywhere else.
"""

import argparse
import base64
import hashlib
import json
import sys
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func
from sqlmodel import select

from store import db, migrate, repo
from store.errors import StoreError
from store.fixtures import mock
from store.models import Study
from store.states import (Action, BindingSource, EgressState, SubjectType,
                          Verdict)

UNSIGNED = "unsigned-fixture"
SYSTEM_USER = "agent"


def _bytes(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _b64(number):
    return base64.urlsafe_b64encode(number.to_bytes(32, "big")).rstrip(b"=").decode()


def _public_jwk():
    """A P-256 public key. The private half is discarded on the spot."""
    numbers = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": _b64(numbers.x), "y": _b64(numbers.y)}


def _mock_digest(ends, seed):
    """A digest with the ends the mockups show, 4be1…90c3."""
    head, tail = ends
    middle = hashlib.sha256(seed.encode()).hexdigest()[:56]
    return f"sha256:{head}{middle}{tail}"


def _decide(session, subject_type, subject_id, action, user, at, *, subject_hash=None,
            reason=None, dac_confirmed=None):
    document = {
        "schema": "biochef.node-decision.v1", "node": mock.NODE,
        "subject_type": subject_type.value, "subject_id": str(subject_id),
        "subject_hash": subject_hash, "action": action.value, "user": user,
        "at": at.isoformat(timespec="seconds"), "reason": reason,
    }
    if dac_confirmed is not None:
        document["dac_confirmed"] = dac_confirmed
    return repo.record_decision(
        session, subject_type=subject_type, subject_id=subject_id, action=action,
        user=user, object_raw=_bytes(document), signature=UNSIGNED, at=at,
        subject_hash=subject_hash, reason=reason, dac_confirmed=dac_confirmed)


def _fixture_token(name, ls_aai_id):
    """Shaped like an ID token, and signed by nobody."""
    claims = json.dumps({"sub": ls_aai_id, "name": name}).encode()
    payload = base64.urlsafe_b64encode(claims).rstrip(b"=").decode()
    return f"fixture.{payload}.unsigned"


def _token_claims(token):
    """What the verifier of section 7.3 will hand over, for a fixture token.

    The display name in a seeded database comes from here, so that it takes the
    same road as a real one: out of the token, and from nowhere else.
    """
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _seed_studies(session, now):
    versions = {}
    previous = {}
    for (study, number, goal, proposer, names, dataset, nodes, rules, age,
         state) in mock.STUDY_VERSIONS:
        at = now - age
        members = [mock.MEMBERS[name] for name in names]
        entries = [{"member": member, "ls_aai_id": ls_aai_id}
                   for member, ls_aai_id in members]
        node_entries = [{"node": node, "datasets": [dataset]} for node in nodes]
        proposer_id = mock.MEMBERS[proposer][0]
        document = {"study_id": study, "version": number, "goal": goal,
                    "proposer": proposer_id, "members": entries,
                    "nodes": node_entries, "output_rules": rules,
                    "previous_digest": previous.get(study)}
        raw = _bytes(document)
        digest = _digest(raw)
        row = repo.add_study_version(
            session, study_id=study, version=number, digest=digest, raw=raw,
            goal=goal, proposer=proposer_id, members=entries, nodes=node_entries,
            datasets=[dataset], output_rules=rules,
            previous_digest=previous.get(study), now=at)
        previous[study] = digest
        versions[(study, number)] = row

        for name, (member, ls_aai_id) in zip(names, members):
            token = _fixture_token(name, ls_aai_id)
            repo.add_member_key(
                session, study_version_id=row.id, member=member,
                ls_aai_id=ls_aai_id, signing_key=_public_jwk(),
                result_key=f"age1fixture{member}", binding_token=token)
            session.flush()
            error = mock.UNVERIFIED.get((study, number, name))
            if error is None:
                repo.mark_identity_verified(session, row.id, member,
                                            _token_claims(token))
            else:
                repo.mark_identity_failed(session, row.id, member, error)
        session.flush()
        _apply_study_state(session, row, state, at)
    return versions


def _apply_study_state(session, row, state, at):
    subject = SubjectType.STUDY_VERSION
    decided = at + timedelta(hours=1)
    if state == "pending":
        return
    if state in ("approved", "superseded", "withdrawn"):
        replaced = repo.approve_study_version(session, row.id, now=decided)
        _decide(session, subject, row.id, Action.APPROVE, "j.silva", decided,
                subject_hash=row.digest, dac_confirmed=True)
        if replaced is not None:
            _decide(session, subject, replaced.id, Action.WITHDRAW, SYSTEM_USER,
                    decided, subject_hash=replaced.digest, reason="superseded")
    if state == "withdrawn":
        withdrawn = decided + timedelta(days=5)
        repo.withdraw_study_version(session, row.id, now=withdrawn)
        _decide(session, subject, row.id, Action.WITHDRAW, "j.silva", withdrawn,
                subject_hash=row.digest)


def _seed_workflows(session, versions, now):
    workflows = {}
    for (key, study, number, name, version, ends, values, state,
         age) in mock.WORKFLOWS:
        at = now - age
        digest = _mock_digest(ends, key)
        steps = [{"id": step, "repo": f"biochef-plugins-{step}",
                  "digest": _mock_digest(("0e5f", "92dd"), step),
                  "signature": "sign/v1", "paramValues": dict(values)}
                 for step in mock.GWAS_STEPS]
        document = {"nodes": [{"id": step["id"], "type": "workflowNode",
                               "data": {"repo": f'{step["repo"]}@{step["digest"]}',
                                        "paramValues": step["paramValues"]}}
                              for step in steps], "edges": []}
        row = repo.add_workflow_version(
            session, study_version_id=versions[(study, number)].id, digest=digest,
            raw=_bytes(document), steps=steps, name=name, version=version, now=at)
        workflows[key] = row
        if state == "approved":
            decided = at + timedelta(hours=1)
            repo.approve_workflow_version(session, row.id, now=decided)
            _decide(session, SubjectType.WORKFLOW_VERSION, row.id, Action.APPROVE,
                    "j.silva", decided, subject_hash=digest)
    return workflows


def _add_request(session, versions, workflows, now, *, request_id, study, number,
                 key, member, age, joined=None):
    at = now - age
    member = mock.MEMBERS[member][0]
    version = versions[(study, number)]
    # A repeated request carries the same payload as the one it joined.
    payload = _digest((joined or request_id).encode())
    document = {"request_id": request_id, "study_id": study,
                "study_version": number, "study_digest": version.digest,
                "workflow_digest": workflows[key].digest, "member": member,
                "node": mock.NODE, "datasets": version.datasets,
                "not_after": (at + timedelta(days=1)).isoformat(),
                "payload_digest": payload}
    repo.add_request(
        session, request_id=request_id, study_id=study, study_version=number,
        study_digest=version.digest, workflow_digest=workflows[key].digest,
        member=member, node=mock.NODE, datasets=version.datasets,
        not_after=at + timedelta(days=1), payload_digest=payload,
        raw=_bytes(document), now=at)
    return at


def _seed_requests(session, versions, workflows, now):
    pending_verdicts = []
    for (request_id, study, number, key, member, age, verdict, failed, joined,
         run) in mock.REQUESTS:
        at = _add_request(
            session, versions, workflows, now, request_id=request_id, study=study,
            number=number, key=key, member=member, age=age, joined=joined)
        pending_verdicts.append((request_id, at, verdict, failed, joined, run))

    pending_verdicts.extend(_older_requests(session, versions, workflows, now))

    # Joined requests point at a request that must exist first, and every row
    # above does by now.
    for request_id, at, verdict, failed, joined, run in pending_verdicts:
        repo.set_verdict(session, request_id, Verdict(verdict), failed_check=failed,
                         joined_request_id=joined, run_id=run)
        if verdict == "refused":
            _decide(session, SubjectType.REQUEST, request_id, Action.REFUSE,
                    SYSTEM_USER, at, reason=failed)


def _older_requests(session, versions, workflows, now):
    """Generated to reach the counts on mock 02. All older than the named ones."""
    defaults = {mock.CARDIO: (2, "gwas-maf01", "Rui Matos"),
                mock.SEPSIS: (1, "sepsis-cohort", "Marta Reis")}
    rows = []
    for study, wanted in mock.STUDY_REQUEST_COUNTS.items():
        number, key, member = defaults[study]
        named = sum(1 for row in mock.REQUESTS if row[1] == study)
        for index in range(wanted - named):
            request_id = f"req_{study[:2]}{index:02d}"
            age = mock.days(6 + index)
            at = _add_request(
                session, versions, workflows, now, request_id=request_id,
                study=study, number=number, key=key, member=member, age=age)
            rows.append((request_id, at, "accepted", None, None,
                         f"run_{request_id[4:]}"))
    return rows


KEEP_SECONDS = 3600
"""Retention of the seeded runs, the default of BIOCHEF_KEEP_OUTPUTS."""


def _finish_egress(session, run_id, now, *stages):
    for stage in stages:
        repo.set_egress_state(session, run_id, stage, now,
                              retain_seconds=KEEP_SECONDS)


def _seed_runs(session, now):
    start = now - mock.hours(6)
    steps = []
    moment = start
    for name, minutes in mock.STEP_MINUTES.items():
        end = moment + timedelta(minutes=minutes)
        steps.append({"id": name, "state": "COMPLETE",
                      "started_at": moment.isoformat(), "ended_at": end.isoformat()})
        moment = end

    repo.save_run(session, "run_8f21", state="COMPLETE", request_id="req_8f21",
                  steps=steps, started_at=start, ended_at=moment, exit_code=0,
                  workspace_path="/var/lib/biochef/runs/run_8f21",
                  manifest_path="/var/lib/biochef/runs/run_8f21/run.json",
                  log_stdout="fixture log\n",
                  egress_files=list(mock.OUTPUT_FILES),
                  airlock_ticket="airlock-0001",
                  airlock_at=moment + timedelta(minutes=1))
    _finish_egress(session, "run_8f21", moment, EgressState.RULES_PENDING,
                   EgressState.AWAITING_AIRLOCK)

    repo.save_run(session, "run_8e90", state="RUNNING", request_id="req_8e90",
                  started_at=now - mock.hours(1),
                  steps=[{"id": "summarise", "state": "RUNNING",
                          "started_at": (now - mock.hours(1)).isoformat(),
                          "ended_at": None}],
                  log_stdout="fixture log\n")

    finished = [(row[0], row[9]) for row in mock.REQUESTS
                if row[9] and row[9] not in ("run_8f21", "run_8e90")]
    finished += [(f"req_{run[4:]}", run) for run in _generated_runs(session)]
    for request_id, run_id in finished:
        ended = now - mock.hours(19)
        repo.save_run(session, run_id, state="COMPLETE", request_id=request_id,
                      started_at=ended - mock.hours(1), ended_at=ended, exit_code=0)
        _finish_egress(session, run_id, ended, EgressState.RULES_PENDING,
                       EgressState.AWAITING_AIRLOCK, EgressState.RELEASED,
                       EgressState.SENT)


def _generated_runs(session):
    named = {row[0] for row in mock.REQUESTS}
    rows = []
    for study in mock.STUDY_REQUEST_COUNTS:
        rows += [row.run_id for row in
                 repo.requests_of_study(session, study, limit=100)
                 if row.request_id not in named]
    return rows


def _seed_bindings(session, now):
    for dataset_id, source, location, by in mock.BINDINGS:
        at = now - mock.days(2)
        repo.bind_dataset(session, dataset_id=dataset_id,
                          source=BindingSource(source), type="localpath",
                          location=location, bound_by=by, now=at)
        if source == "page":
            _decide(session, SubjectType.DATASET_BINDING, dataset_id, Action.BIND,
                    by, at)


def _seed_node(session, now):
    nodes = [{"node": node} for node in mock.FEDERATION_NODES]
    repo.store_federation_list(
        session, version=mock.FEDERATION_VERSION,
        raw=_bytes({"version": mock.FEDERATION_VERSION, "nodes": nodes}),
        signature=UNSIGNED, nodes=nodes, now=now - mock.days(1))
    repo.record_poll(session, ok=True, now=now)


def seed(bind=None, now=None):
    """Fill the database. Refuses one that is behind or already holds data."""
    bind = bind or db.engine()
    if not migrate.is_current(bind):
        raise StoreError("the schema is not current; run: python -m store migrate")
    now = now or repo.utcnow()
    with db.writing(bind) as session:
        if session.exec(select(func.count()).select_from(Study)).one():
            raise StoreError("this database already holds data; the seed only "
                             "fills an empty one")
        versions = _seed_studies(session, now)
        workflows = _seed_workflows(session, versions, now)
        _seed_requests(session, versions, workflows, now)
        _seed_runs(session, now)
        _seed_bindings(session, now)
        _seed_node(session, now)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m store.seed")
    parser.add_argument("--fixture", required=True, choices=["mock"])
    parser.parse_args(argv)
    try:
        seed()
    except StoreError as refused:
        print(refused, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
