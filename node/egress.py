"""How a finished run's results leave, or do not (section 6.5).

    output rules, then the airlock or a person, then encrypt and sign, then post

Files held back by the output rules never leave, in either mode, and that is
decided once, here, before anyone reviews anything. A release covers the passing
files only.

The Policy Engine, the TRE's airlock and the coordinator's result endpoint are not
there yet, so all three are protocols with fakes for development and tests. With
none of them a run waits rather than leaves: a missing piece stops egress, and
nothing skips a stage to keep things moving.

A withheld result still gets an answer. The signed notice is made when the result
is withheld, by a person or by the airlock, and kept as a message waiting to be
sent: what goes out is what was decided then. Sending it is part 8.
"""

import base64
import hashlib
import io
import tarfile
from typing import Protocol

import audit
from node import decisions
from store import repo
from store.errors import AlreadyPosted, Conflict
from store.states import Action, EgressState, MessageKind, SubjectType

AIRLOCK = "airlock"
MANUAL = "manual"

PASS = "pass"
HELD_BACK = "held_back"
PENDING = "pending"
RELEASED = "released"
WITHHELD = "withheld"

EVENT_NOTICE = "node.egress.notice"


class Airlock(Protocol):
    def submit(self, files) -> str:
        ...

    def status(self, ticket) -> str:
        """`pending`, `released` or `withheld`."""


class FakeAirlock:
    """Holds everything until a test or a developer answers."""

    def __init__(self):
        self.submitted = {}
        self.answers = {}

    def submit(self, files):
        ticket = f"fake-{len(self.submitted) + 1}"
        self.submitted[ticket] = list(files)
        return ticket

    def status(self, ticket):
        return self.answers.get(ticket, PENDING)


class Coordinator(Protocol):
    def post_result(self, partial) -> None:
        ...


class FakeCoordinator:
    def __init__(self):
        self.posted = []

    def post_result(self, partial):
        self.posted.append(partial)


def passing(row):
    return [entry for entry in row.egress_files if entry.get("verdict") == PASS]


def _retain_until(session, run_id, state, now, keep_seconds):
    repo.set_egress_state(session, run_id, state, now, retain_seconds=keep_seconds)


def start(session, run_id, policy, airlock, *, mode, now=None, keep_seconds=None):
    """Judge a finished run's files, and send it to whoever reviews it next."""
    now = now or repo.utcnow()
    run = repo.get_run(session, run_id)
    request = repo.get_request(session, run.request_id)
    version = repo.study_version_named(session, request.study_id, request.study_version)
    repo.set_egress_state(session, run_id, EgressState.RULES_PENDING, now)
    verdicts = {row.file: row for row in policy.check(run, version.output_rules)}
    judged = []
    for entry in run.egress_files:
        verdict = verdicts.get(entry["file"])
        # A file the engine said nothing about stays: only a pass lets one out.
        judged.append({**entry,
                       "verdict": verdict.verdict if verdict else HELD_BACK,
                       "reason": verdict.reason if verdict else
                       "the output rules did not cover this file"})
    repo.save_run(session, run_id, state=run.state, egress_files=judged)
    if mode == AIRLOCK:
        ticket = airlock.submit([entry["file"] for entry in passing(run)])
        repo.save_run(session, run_id, state=run.state, airlock_ticket=ticket,
                      airlock_at=now)
        repo.set_egress_state(session, run_id, EgressState.AWAITING_AIRLOCK, now)
    else:
        repo.set_egress_state(session, run_id, EgressState.AWAITING_REVIEW, now)


def _recipient_key(session, request):
    """The requester's age key, from the study version the request named."""
    version = repo.study_version_named(session, request.study_id, request.study_version)
    return next(row for row in repo.member_keys(session, version.id)
                if row.member == request.member).result_key


def keep_withheld_notice(session, keys, run_id, *, node, now=None):
    """The signed answer for a result that does not leave, kept until it is sent."""
    decisions.require_a_trail()
    now = now or repo.utcnow()
    run = repo.get_run(session, run_id)
    request = repo.get_request(session, run.request_id)
    document = partial(keys, request, _recipient_key(session, request), WITHHELD,
                       node=node)
    row = repo.add_message(session, run_id, MessageKind.WITHHELD_RESULT,
                           decisions.canonical(document), now)
    audit.record(EVENT_NOTICE, run_id=run_id, request_id=request.request_id,
                 message_id=row.id, decision=WITHHELD, signed_by=keys.signing_fingerprint)
    return row


def poll_airlock(session, airlock, keys, *, node, now=None, keep_seconds=None):
    now = now or repo.utcnow()
    for run in repo.runs_in_egress_state(session, EgressState.AWAITING_AIRLOCK):
        answer = airlock.status(run.airlock_ticket)
        if answer == RELEASED:
            repo.set_egress_state(session, run.run_id, EgressState.RELEASED, now)
        elif answer == WITHHELD:
            repo.set_egress_state(session, run.run_id, EgressState.WITHHELD, now,
                                  retain_seconds=keep_seconds)
            keep_withheld_notice(session, keys, run.run_id, node=node, now=now)


def _decide(session, keys, node, run_id, action, user, now):
    return decisions.decide(session, keys, node=node, subject_type=SubjectType.EGRESS,
                            subject_id=run_id, action=action, user=user, at=now)


def release(session, keys, *, node, run_id, user, now=None):
    """A person lets the passing files go. Manual mode only, and signed."""
    now = now or repo.utcnow()
    repo.set_egress_state(session, run_id, EgressState.RELEASED, now)
    return _decide(session, keys, node, run_id, Action.RELEASE, user, now)


def withhold(session, keys, *, node, run_id, user, now=None, keep_seconds=None):
    now = now or repo.utcnow()
    repo.set_egress_state(session, run_id, EgressState.WITHHELD, now,
                          retain_seconds=keep_seconds)
    decision = _decide(session, keys, node, run_id, Action.WITHHOLD, user, now)
    keep_withheld_notice(session, keys, run_id, node=node, now=now)
    return decision


def undo_review(session, decision, *, user):
    """Take back a release or a withholding that has not gone anywhere yet."""
    run = repo.get_run(session, decision.subject_id)
    if decision.posted_to_coordinator_at is not None:
        raise Conflict("This decision has already been sent, so it cannot be undone.")
    expected = (EgressState.RELEASED.value if decision.action == Action.RELEASE.value
                else EgressState.WITHHELD.value)
    if run.egress_state != expected:
        raise Conflict("This decision cannot be undone: the run has moved on.")
    if decision.action == Action.WITHHOLD.value:
        try:
            repo.delete_unsent_message(session, run.run_id, MessageKind.WITHHELD_RESULT)
        except AlreadyPosted:
            raise Conflict("The requester has already been told the result was "
                           "withheld, so this cannot be undone.") from None
    repo.reopen_review(session, run.run_id)
    decisions.undo(session, decision.id, user=user)


def _bundle(names, read):
    """The passing files as one tar, with their digests for the statement."""
    buffer = io.BytesIO()
    subjects = []
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in names:
            data = read(name)
            subjects.append({"name": name,
                             "digest": {"sha256": hashlib.sha256(data).hexdigest()}})
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), subjects


def partial(keys, request, recipient_key, decision, *, node, names=(), read=None,
            encrypt=None):
    """The result document of ieeta-pt/biochef-coordinator#6, signed with the node key."""
    subjects, ciphertext = [], None
    if decision == RELEASED:
        data, subjects = _bundle(names, read)
        ciphertext = base64.b64encode(encrypt(data, recipient_key)).decode()
    fingerprint = "sha256:" + hashlib.sha256(recipient_key.encode()).hexdigest()
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://biochef.app/partial/v1",
        "predicate": {
            "request_id": request.request_id, "task_id": request.request_id,
            "attempt": 1, "study_id": request.study_id,
            "study_version": request.study_version,
            "study_digest": request.study_digest,
            "workflow_digest": request.workflow_digest, "node": node,
            "ciphertext_size": len(ciphertext) if ciphertext else None,
            "recipient_key": fingerprint},
    }
    _, signature = decisions.sign(statement, keys)
    return {"node": node, "attempt": 1, "decision": decision, "statement": statement,
            "signature": signature, "ciphertext": ciphertext}


def _encrypt(data, recipient_key):
    import pyrage
    return pyrage.encrypt(data, [pyrage.x25519.Recipient.from_str(recipient_key)])


def send_released(session, keys, coordinator, workspace_of, *, node, now=None,
                  keep_seconds=None):
    """Stages 3 and 4: encrypt for the requester, sign, post."""
    now = now or repo.utcnow()
    for run in repo.runs_in_egress_state(session, EgressState.RELEASED):
        ws = workspace_of(run.run_id)
        if ws is None:
            continue
        request = repo.get_request(session, run.request_id)

        def read(name):
            with ws.open_read(name) as handle:
                return handle.read()

        names = [entry["file"] for entry in passing(run)]
        coordinator.post_result(partial(keys, request,
                                        _recipient_key(session, request), RELEASED,
                                        node=node, names=names, read=read,
                                        encrypt=_encrypt))
        repo.set_egress_state(session, run.run_id, EgressState.SENT, now,
                              retain_seconds=keep_seconds)
