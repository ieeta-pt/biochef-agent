"""Every read and write of the store goes through here.

Each function takes a session and leaves the transaction to the caller, because
the rules that matter span several writes: an approval, the version it replaces
and the signed decisions for both must land together or not at all.

A time is an argument wherever it decides something, for the same reason
node/verify.py will take `now`: a rule about time should be testable without
waiting for it.
"""

import unicodedata
from datetime import datetime, timezone

from sqlalchemy import delete, func, update
from sqlmodel import select

from store.errors import (AlreadyPosted, Conflict, IdentityNotVerified,
                          IllegalTransition, NotFound, StaleList)
from store.models import (DatasetBinding, Decision, FederationList, MemberKey,
                          NodeStatus, OutgoingMessage, Request, RunSummary, Study,
                          StudyVersion, TesListener, TesTask, WorkflowVersion)
from store.states import (BUSY_RUN_STATES, CANCELLABLE_RUN_STATES,
                          EGRESS_ALLOWED, EGRESS_FINISHED, STUDY_ALLOWED,
                          WORKFLOW_ALLOWED, BindingSource, EgressState,
                          Action, MessageKind, RefusalCode, SubjectType,
                          VersionState, Verdict)


SHORT_ID_LENGTH = 8
"""How much of an ls_aai_id the page shows when a member has no verified name."""

MAX_NAME_LENGTH = 128
"""Longer than any real name. A longer claim is shown as no name at all."""

_INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}
"""Control and format characters, and line and paragraph separators: what can
reorder, hide or break the text around a name on the page where approvals
happen (U+202E, zero-width joiners, DEL, U+2028)."""


def utcnow():
    return datetime.now(timezone.utc)


def _get(session, model, key):
    row = session.get(model, key)
    if row is None:
        raise NotFound(f"no {model.__tablename__} {key!r}")
    return row


def _move(row, table, state, now):
    current = VersionState(row.state)
    if state not in table[current]:
        raise IllegalTransition(
            f"{row.__tablename__} {row.id}: {current.value} -> {state.value}"
        )
    row.state = state.value
    row.state_changed_at = now


# Study versions


def add_study_version(session, *, study_id, version, digest, raw, goal, proposer,
                      members, nodes, datasets, output_rules,
                      previous_digest=None, now=None):
    """Record a study version as received, pending a decision.

    Receiving the same document twice is normal, since polling repeats itself,
    and returns the row already there. The same version number with different
    bytes is not normal: someone is offering two documents under one name.
    """
    now = now or utcnow()
    found = session.exec(select(StudyVersion).where(
        StudyVersion.study_id == study_id,
        StudyVersion.version == version)).first()
    if found is not None:
        if found.digest != digest or found.raw != raw:
            raise Conflict(f"study {study_id} v{version} differs from the one "
                           f"already received")
        return found

    if session.get(Study, study_id) is None:
        session.add(Study(id=study_id, created_at=now))
    # A poll can deliver a version after a newer one was approved here. It could
    # never be approved, so it arrives superseded rather than waiting for ever.
    approved = approved_version(session, study_id)
    state = (VersionState.SUPERSEDED if approved is not None
             and approved.version > version else VersionState.PENDING)
    row = StudyVersion(
        study_id=study_id, version=version, digest=digest, raw=raw, goal=goal,
        proposer=proposer, members=members, nodes=nodes, datasets=datasets,
        output_rules=output_rules, previous_digest=previous_digest,
        received_at=now, state=state.value, state_changed_at=now)
    session.add(row)
    session.flush()
    return row


def get_study_version(session, version_id):
    return _get(session, StudyVersion, version_id)


def study_versions(session, study_id):
    """Every version of a study, newest first, whatever became of it."""
    return list(session.exec(
        select(StudyVersion).where(StudyVersion.study_id == study_id)
        .order_by(StudyVersion.version.desc())))


def study_version_named(session, study_id, version):
    return session.exec(select(StudyVersion).where(
        StudyVersion.study_id == study_id,
        StudyVersion.version == version)).first()


def approved_version(session, study_id):
    return session.exec(select(StudyVersion).where(
        StudyVersion.study_id == study_id,
        StudyVersion.state == VersionState.APPROVED.value)).first()


def study_heading(session, version):
    """How the page names a study until study versions carry a title."""
    title = _get(session, Study, version.study_id).title
    if title:
        return title
    lines = version.goal.strip().splitlines()
    return f"{version.study_id} · {lines[0]}" if lines else version.study_id


def pending_study_versions(session):
    return list(session.exec(
        select(StudyVersion)
        .where(StudyVersion.state == VersionState.PENDING.value)
        .order_by(StudyVersion.received_at.desc())))


def add_member_key(session, *, study_version_id, member, ls_aai_id, signing_key,
                   result_key, binding_token):
    """Record a member's keys as the study version lists them.

    Idempotent like add_study_version, because the poll that adds them repeats.
    Different keys for the same member of the same version are refused: the
    version is signed, so its keys cannot change.
    """
    fields = {"ls_aai_id": ls_aai_id, "signing_key": signing_key,
              "result_key": result_key, "binding_token": binding_token}
    found = session.get(MemberKey, (study_version_id, member))
    if found is not None:
        if any(getattr(found, name) != value for name, value in fields.items()):
            raise Conflict(f"the keys of {member} in study version "
                           f"{study_version_id} differ from the ones received")
        return found
    row = MemberKey(study_version_id=study_version_id, member=member, **fields)
    session.add(row)
    return row


def member_keys(session, study_version_id):
    return list(session.exec(select(MemberKey).where(
        MemberKey.study_version_id == study_version_id)
        .order_by(MemberKey.member)))


def _usable_name(claims):
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if any(unicodedata.category(character) in _INVISIBLE for character in name):
        return None
    name = name.strip()
    return name if len(name) <= MAX_NAME_LENGTH else None


def mark_identity_verified(session, study_version_id, member, claims):
    """Record a member's verified identity, from the claims of their token.

    The only way a name enters the store. `member` is an opaque id and the study
    document is written by whoever proposed the study, so a name from either
    would be a claim nobody checked. `claims` come from the binding_token after
    the checks of section 7.3 passed, and they must be about this member.
    """
    row = _get(session, MemberKey, (study_version_id, member))
    if claims.get("sub") != row.ls_aai_id:
        raise Conflict(f"the token of {member} names someone else")
    row.identity_verified = True
    row.identity_error = None
    row.display_name = _usable_name(claims)
    return row


def mark_identity_failed(session, study_version_id, member, error):
    """A failed check leaves no name: nothing vouches for one any more."""
    row = _get(session, MemberKey, (study_version_id, member))
    row.identity_verified = False
    row.identity_error = error
    row.display_name = None
    return row


def member_label(row):
    """How the page names a member: never by the opaque `member` id."""
    if row.display_name:
        return row.display_name
    local, at, domain = row.ls_aai_id.partition("@")
    if len(local) <= SHORT_ID_LENGTH:
        return row.ls_aai_id
    return f"{local[:SHORT_ID_LENGTH]}…{at}{domain}"


def _require_verified_members(session, version):
    keys = {row.member: row for row in member_keys(session, version.id)}
    listed = [entry["member"] for entry in version.members]
    if not listed:
        raise IdentityNotVerified(f"study version {version.id} lists no members")
    for member in listed:
        row = keys.get(member)
        if row is None or not row.identity_verified:
            raise IdentityNotVerified(
                f"study version {version.id}: {member} is not verified")


def _end_workflows(session, study_version_id, state, now):
    """Move the workflows still open under a study version along with it."""
    for workflow in workflow_versions(session, study_version_id):
        if workflow.state in (VersionState.PENDING.value,
                              VersionState.APPROVED.value):
            _move(workflow, WORKFLOW_ALLOWED, state, now)


def approve_study_version(session, version_id, now=None):
    """Approve a version, superseding the approved one in the same transaction.

    Returns the version it replaced, or None. Its workflows go with it, because
    approvals do not carry over to a new study version. Older versions still
    pending are superseded too, with their workflows: they could never be
    approved after this one.

    Everything is checked before anything moves, so a refusal leaves the store
    as it was even for a caller that catches the error and commits.
    """
    now = now or utcnow()
    version = get_study_version(session, version_id)
    current = VersionState(version.state)
    if VersionState.APPROVED not in STUDY_ALLOWED[current]:
        raise IllegalTransition(f"study_version {version.id}: {current.value} -> "
                                f"{VersionState.APPROVED.value}")
    _require_verified_members(session, version)
    replaced = approved_version(session, version.study_id)
    if replaced is not None and replaced.version > version.version:
        raise Conflict(f"study {version.study_id}: v{replaced.version} is "
                       f"approved, so v{version.version} cannot replace it")

    if replaced is not None:
        _move(replaced, STUDY_ALLOWED, VersionState.SUPERSEDED, now)
        _end_workflows(session, replaced.id, VersionState.SUPERSEDED, now)
        # The index allows one approved row at any instant, and a flush orders
        # updates by key rather than by what they mean.
        session.flush()
    for older in study_versions(session, version.study_id):
        if (older.version < version.version
                and older.state == VersionState.PENDING.value):
            _move(older, STUDY_ALLOWED, VersionState.SUPERSEDED, now)
            _end_workflows(session, older.id, VersionState.SUPERSEDED, now)

    _move(version, STUDY_ALLOWED, VersionState.APPROVED, now)
    session.flush()
    return replaced


def refuse_study_version(session, version_id, now=None):
    version = get_study_version(session, version_id)
    _move(version, STUDY_ALLOWED, VersionState.REFUSED, now or utcnow())
    return version


def withdraw_study_version(session, version_id, now=None):
    """Withdraw a version, and the workflows still open under it."""
    now = now or utcnow()
    version = get_study_version(session, version_id)
    _move(version, STUDY_ALLOWED, VersionState.WITHDRAWN, now)
    _end_workflows(session, version.id, VersionState.WITHDRAWN, now)
    return version


def studies_with_versions(session):
    """Every study id with at least one version, oldest first."""
    return list(session.exec(select(Study.id).order_by(Study.created_at, Study.id)))


def restore_version(session, row, state, now=None):
    """Put a version back where an undone decision found it.

    Outside the state tables on purpose: those say what may happen next, and
    this says that something did not happen after all. Only node/review.py calls
    it, after checking that nothing has been built on the decision.
    """
    row.state = state.value
    row.state_changed_at = now or utcnow()
    session.flush()
    return row


def versions_replaced_at(session, version):
    """The approved version this one superseded when it was approved, if any.

    Older versions still pending are superseded at the same moment; the one that
    was approved is told apart by the withdrawal written for it. A version that
    was only ever pending has no decision at all.
    """
    for row in session.exec(select(StudyVersion).where(
            StudyVersion.study_id == version.study_id,
            StudyVersion.state == VersionState.SUPERSEDED.value,
            StudyVersion.state_changed_at == version.state_changed_at)):
        withdrawals = session.exec(select(func.count()).select_from(Decision).where(
            Decision.subject_type == SubjectType.STUDY_VERSION.value,
            Decision.subject_id == str(row.id),
            Decision.action == Action.WITHDRAW.value)).one()
        if withdrawals:
            return row
    return None


# Workflow versions


def add_workflow_version(session, *, study_version_id, digest, raw, steps,
                         name=None, version=None, workflowhub_id=None, now=None):
    """Record a workflow version as received, pending a decision."""
    now = now or utcnow()
    study_version = get_study_version(session, study_version_id)
    found = session.exec(select(WorkflowVersion).where(
        WorkflowVersion.study_version_id == study_version_id,
        WorkflowVersion.digest == digest)).first()
    if found is not None:
        if found.raw != raw:
            raise Conflict(f"workflow {digest} differs from the one received")
        return found

    row = WorkflowVersion(
        study_id=study_version.study_id, study_version_id=study_version_id,
        digest=digest, raw=raw, steps=steps, name=name, version=version,
        workflowhub_id=workflowhub_id, received_at=now, state_changed_at=now)
    session.add(row)
    session.flush()
    return row


def get_workflow_version(session, workflow_id):
    return _get(session, WorkflowVersion, workflow_id)


def workflow_versions(session, study_version_id):
    return list(session.exec(
        select(WorkflowVersion)
        .where(WorkflowVersion.study_version_id == study_version_id)
        .order_by(WorkflowVersion.received_at.desc(),
                  WorkflowVersion.id.desc())))


def pending_workflow_versions(session):
    return list(session.exec(
        select(WorkflowVersion)
        .where(WorkflowVersion.state == VersionState.PENDING.value)
        .order_by(WorkflowVersion.received_at.desc())))


def approved_workflow(session, study_version_id, digest):
    return session.exec(select(WorkflowVersion).where(
        WorkflowVersion.study_version_id == study_version_id,
        WorkflowVersion.digest == digest,
        WorkflowVersion.state == VersionState.APPROVED.value)).first()


def _decide_workflow(session, workflow_id, state, now):
    workflow = get_workflow_version(session, workflow_id)
    _move(workflow, WORKFLOW_ALLOWED, state, now or utcnow())
    return workflow


def approve_workflow_version(session, workflow_id, now=None):
    """Approve a workflow under its study version, which must itself be approved."""
    workflow = get_workflow_version(session, workflow_id)
    study_version = get_study_version(session, workflow.study_version_id)
    if study_version.state != VersionState.APPROVED.value:
        raise Conflict(f"workflow {workflow_id}: study version "
                       f"{study_version.id} is {study_version.state}")
    return _decide_workflow(session, workflow_id, VersionState.APPROVED, now)


def refuse_workflow_version(session, workflow_id, now=None):
    return _decide_workflow(session, workflow_id, VersionState.REFUSED, now)


def withdraw_workflow_version(session, workflow_id, now=None):
    return _decide_workflow(session, workflow_id, VersionState.WITHDRAWN, now)


# Decisions


def record_decision(session, *, subject_type, subject_id, action, user,
                    object_raw, signature, at, subject_hash=None, comment=None,
                    dac_confirmed=None, reason=None):
    row = Decision(subject_type=subject_type.value, subject_id=str(subject_id),
                   subject_hash=subject_hash, action=action.value, user=user,
                   comment=comment, dac_confirmed=dac_confirmed, reason=reason,
                   at=at, object_raw=object_raw, signature=signature)
    session.add(row)
    session.flush()
    return row


def decisions_about(session, subject_type, subject_id):
    return list(session.exec(
        select(Decision)
        .where(Decision.subject_type == subject_type.value,
               Decision.subject_id == str(subject_id))
        .order_by(Decision.at.desc(), Decision.id.desc())))


def get_decision(session, decision_id):
    return _get(session, Decision, decision_id)


def unposted_decisions(session):
    """What the coordinator has not been told yet, oldest first."""
    return list(session.exec(
        select(Decision).where(Decision.posted_to_coordinator_at.is_(None))
        .order_by(Decision.id)))


def has_left(decision):
    """Whether a decision may be at the coordinator, so it can no longer be undone.

    True from the moment its POST is claimed, not only once the POST returned:
    a POST whose answer was lost may still have arrived.
    """
    return (decision.posting_started_at is not None
            or decision.posted_to_coordinator_at is not None)


def claim_for_posting(session, decision_id, now=None):
    """Mark a decision as on its way, before the POST that sends it.

    The caller commits this in its own transaction and only then sends, so an
    undo either ran first, and the decision is gone (NotFound: send nothing), or
    runs after and is refused. A decision claimed before and never marked posted,
    because the process stopped, is returned again so the POST can be retried.
    One statement with the condition in it, like delete_unposted_decision.
    """
    session.exec(update(Decision).where(
        Decision.id == decision_id, Decision.posting_started_at.is_(None))
        .values(posting_started_at=now or utcnow()))
    row = _get(session, Decision, decision_id)
    session.refresh(row)
    return row


def mark_posted(session, decision_id, now=None):
    row = _get(session, Decision, decision_id)
    if row.posted_to_coordinator_at is not None:
        raise Conflict(f"decision {decision_id} was already posted")
    now = now or utcnow()
    if row.posting_started_at is None:
        # Posted implies on its way: an undo must see it either way.
        row.posting_started_at = now
    row.posted_to_coordinator_at = now
    return row


def delete_unposted_decision(session, decision_id):
    """Undo a decision that has not left this node. Refused once it may have.

    One statement with the condition in it, so that a poll claiming the decision
    at the same moment cannot slip between a check and a delete.
    """
    result = session.exec(delete(Decision).where(
        Decision.id == decision_id,
        Decision.posting_started_at.is_(None),
        Decision.posted_to_coordinator_at.is_(None)))
    if result.rowcount == 0:
        _get(session, Decision, decision_id)
        raise AlreadyPosted(f"decision {decision_id} may already be at the "
                            f"coordinator")


# Requests


def add_request(session, *, request_id, study_id, study_version, study_digest,
                workflow_digest, member, node, datasets, not_after,
                payload_digest, raw, now=None):
    """Record a request as received, before any check has run."""
    found = session.get(Request, request_id)
    if found is not None:
        if found.raw != raw:
            raise Conflict(f"request {request_id} differs from the one received")
        return found
    row = Request(
        request_id=request_id, study_id=study_id, study_version=study_version,
        study_digest=study_digest, workflow_digest=workflow_digest,
        member=member, node=node, datasets=datasets, not_after=not_after,
        payload_digest=payload_digest, raw=raw, received_at=now or utcnow())
    session.add(row)
    session.flush()
    return row


def get_request(session, request_id):
    return _get(session, Request, request_id)


def set_verdict(session, request_id, verdict, *, failed_check=None, run_id=None):
    """Record the outcome of the checks, once.

    The combinations are refused here, so a screen never has to work out what a
    refusal with no reason was supposed to mean.
    """
    row = get_request(session, request_id)
    if row.verdict is not None:
        raise Conflict(f"request {request_id} already has a verdict")
    if (verdict is Verdict.REFUSED) != (failed_check is not None):
        raise Conflict("a refusal needs a failed check, and only a refusal has one")
    if failed_check is not None:
        failed_check = RefusalCode(failed_check).value
    row.verdict = verdict.value
    row.failed_check = failed_check
    row.run_id = run_id
    return row


def find_request(session, request_id):
    """The request with this id, or None: how a repeat is told from a new one."""
    return session.get(Request, request_id)


def request_as_delivered(*, request_id, study_id, study_version, study_digest,
                         workflow_digest, member, node, datasets, not_after,
                         payload_digest, raw, now=None):
    """A delivery as a Request, never added to the session.

    A repeat is checked like a new request and must not replace the one stored,
    so it is checked as this.
    """
    return Request(
        request_id=request_id, study_id=study_id, study_version=study_version,
        study_digest=study_digest, workflow_digest=workflow_digest, member=member,
        node=node, datasets=datasets, not_after=not_after,
        payload_digest=payload_digest, raw=raw, received_at=now or utcnow())


def note_repeat(session, request_id, now=None):
    """The same request delivered again. Its verdict does not change."""
    row = get_request(session, request_id)
    if row.verdict is None:
        raise Conflict(f"request {request_id} has not been checked yet")
    row.repeats += 1
    row.last_repeat_at = now or utcnow()
    return row


def requests_since(session, since, verdicts=None):
    """Requests received after a moment, newest first."""
    query = select(Request).where(Request.received_at >= since)
    if verdicts:
        query = query.where(Request.verdict.in_([v.value for v in verdicts]))
    return list(session.exec(query.order_by(Request.received_at.desc(),
                                            Request.request_id.desc())))


def requests_of_study(session, study_id, *, limit=20, offset=0):
    return list(session.exec(
        select(Request).where(Request.study_id == study_id)
        .order_by(Request.received_at.desc(), Request.request_id.desc())
        .limit(limit).offset(offset)))


def count_requests_of_study(session, study_id):
    return session.exec(select(func.count()).select_from(Request).where(
        Request.study_id == study_id)).one()


def count_requests_of_workflow(session, study_id, study_version, digest):
    return session.exec(select(func.count()).select_from(Request).where(
        Request.study_id == study_id, Request.study_version == study_version,
        Request.workflow_digest == digest)).one()


def count_requests_run_under(session, study_id, study_version, digest=None):
    """Requests already accepted under a version, or under a workflow."""
    query = select(func.count()).select_from(Request).where(
        Request.study_id == study_id, Request.study_version == study_version,
        Request.verdict == Verdict.ACCEPTED.value)
    if digest is not None:
        query = query.where(Request.workflow_digest == digest)
    return session.exec(query).one()


def count_refused_since(session, since):
    return session.exec(select(func.count()).select_from(Request).where(
        Request.received_at >= since,
        Request.verdict == Verdict.REFUSED.value)).one()


# Runs


def save_run(session, run_id, **fields):
    """Create or update a run's record. Written by the API process only.

    `cancel_requested`, `egress_state` and `retain_until` are not settable here:
    they have their own functions, so that no general update can clear a
    cancellation, skip a stage of egress or move the retention clock. The files
    under review are fixed once the rules have run (models.EGRESS_FIELDS).
    """
    for name in fields:
        if name in ("cancel_requested", "egress_state", "retain_until"):
            raise Conflict(f"{name} is not set through save_run")
        if name not in RunSummary.model_fields:
            raise Conflict(f"run_summary has no field {name!r}")
    row = session.get(RunSummary, run_id)
    if row is None:
        if "state" not in fields:
            raise Conflict(f"a new run {run_id} needs a state")
        row = RunSummary(run_id=run_id, state=fields.pop("state"))
        session.add(row)
    for name, value in fields.items():
        setattr(row, name, value)
    session.flush()
    return row


def get_run(session, run_id):
    return _get(session, RunSummary, run_id)


def request_cancel(session, run_id):
    """Ask the API process to cancel a run. Only that process can do it."""
    row = get_run(session, run_id)
    if row.state not in CANCELLABLE_RUN_STATES:
        raise Conflict(f"run {run_id} is {row.state}, there is nothing to cancel")
    row.cancel_requested = True
    return row


def runs_to_cancel(session):
    return list(session.exec(select(RunSummary).where(
        RunSummary.cancel_requested.is_(True),
        RunSummary.state.in_(CANCELLABLE_RUN_STATES))))


def set_egress_state(session, run_id, state, now=None, *, retain_seconds=None):
    """Move a run through egress.

    The retention clock starts here, at sent or withheld, and not when the run
    ended: a review can outlast any fixed retention.
    """
    row = get_run(session, run_id)
    current = EgressState(row.egress_state)
    if state not in EGRESS_ALLOWED[current]:
        raise IllegalTransition(f"run {run_id}: {current.value} -> {state.value}")
    if state in EGRESS_FINISHED and retain_seconds is None:
        raise Conflict(f"run {run_id}: {state.value} starts the retention clock, "
                       f"so it needs retain_seconds")
    row.egress_state = state.value
    if state in EGRESS_FINISHED:
        moment = (now or utcnow()).timestamp() + retain_seconds
        row.retain_until = datetime.fromtimestamp(moment, timezone.utc)
    return row


def runs_in_egress_state(session, state):
    return list(session.exec(select(RunSummary).where(
        RunSummary.egress_state == state.value)))


def runs_not_finished(session):
    return list(session.exec(select(RunSummary).where(
        RunSummary.state.in_(("QUEUED", "INITIALIZING", "RUNNING", "CANCELING")))))


def runs_ready_for_egress(session):
    """Finished runs of a request that no output rule has looked at yet."""
    return list(session.exec(select(RunSummary).where(
        RunSummary.state == "COMPLETE",
        RunSummary.request_id.is_not(None),
        RunSummary.workspace_path.is_not(None),
        RunSummary.egress_state == EgressState.NOT_READY.value)))


def reopen_review(session, run_id):
    """Undo of a person's release or withholding, and the only way back."""
    row = get_run(session, run_id)
    row.egress_state = EgressState.AWAITING_REVIEW.value
    row.retain_until = None
    return row


def add_message(session, run_id, kind, document, now=None):
    """Keep a signed document for the coordinator. One of each kind per run."""
    get_run(session, run_id)
    row = OutgoingMessage(run_id=run_id, kind=MessageKind(kind).value,
                          document=document, created_at=now or utcnow())
    session.add(row)
    session.flush()
    return row


def message_of(session, run_id, kind):
    return session.exec(select(OutgoingMessage).where(
        OutgoingMessage.run_id == run_id,
        OutgoingMessage.kind == MessageKind(kind).value)).first()


def unsent_messages(session):
    """What is waiting for the coordinator, oldest first. Sending it is part 8."""
    return list(session.exec(select(OutgoingMessage)
                             .where(OutgoingMessage.sent_at.is_(None))
                             .order_by(OutgoingMessage.created_at, OutgoingMessage.id)))


def mark_message_sent(session, message_id, now=None):
    row = _get(session, OutgoingMessage, message_id)
    if row.sent_at is not None:
        raise AlreadyPosted(f"message {message_id} was already sent")
    row.sent_at = now or utcnow()
    return row


def delete_unsent_message(session, run_id, kind):
    """Undo of what the message says. Refused once it has gone, in one statement."""
    result = session.exec(delete(OutgoingMessage).where(
        OutgoingMessage.run_id == run_id,
        OutgoingMessage.kind == MessageKind(kind).value,
        OutgoingMessage.sent_at.is_(None)))
    if result.rowcount == 0 and message_of(session, run_id, kind) is not None:
        raise AlreadyPosted(f"the {kind} message of {run_id} was already sent")


def runs_with_workspaces(session):
    """What the API process needs to rebuild retention after a restart."""
    return list(session.exec(select(RunSummary).where(
        RunSummary.workspace_path.is_not(None))))


def runs_awaiting_egress(session):
    """Runs that must never be evicted, however old they are."""
    waiting = [state.value for state in EgressState
               if state is not EgressState.NOT_READY
               and state not in EGRESS_FINISHED]
    return list(session.exec(select(RunSummary).where(
        RunSummary.egress_state.in_(waiting))))


def count_busy_runs(session):
    return session.exec(select(func.count()).select_from(RunSummary).where(
        RunSummary.state.in_(BUSY_RUN_STATES))).one()


# Dataset bindings


def open_versions(session):
    """Study versions that still count: pending or approved, newest first."""
    return list(session.exec(
        select(StudyVersion)
        .where(StudyVersion.state.in_([VersionState.PENDING.value,
                                       VersionState.APPROVED.value]))
        .order_by(StudyVersion.received_at.desc())))



def bind_dataset(session, *, dataset_id, source, type, location, bound_by,
                 visa_value=None, now=None):
    """Bind a dataset. The config file always wins over the page."""
    row = session.get(DatasetBinding, dataset_id)
    if row is None:
        row = DatasetBinding(dataset_id=dataset_id, source=source.value,
                             type=type, location=location, visa_value=visa_value,
                             bound_by=bound_by, bound_at=now or utcnow())
        session.add(row)
    elif (row.source == BindingSource.CONFIG.value
          and source is BindingSource.PAGE):
        raise Conflict(f"{dataset_id} is bound by the config file")
    else:
        row.source = source.value
        row.type = type
        row.location = location
        row.visa_value = visa_value
        row.bound_by = bound_by
        row.bound_at = now or utcnow()
    session.flush()
    return row


def drop_config_binding(session, dataset_id):
    """Only the config sync does this: page rows go through unbind_dataset."""
    session.delete(_get(session, DatasetBinding, dataset_id))


def unbind_dataset(session, dataset_id):
    row = _get(session, DatasetBinding, dataset_id)
    if row.source == BindingSource.CONFIG.value:
        raise Conflict(f"{dataset_id} is bound by the config file")
    session.delete(row)


def dataset_binding(session, dataset_id):
    return session.get(DatasetBinding, dataset_id)


def dataset_bindings(session):
    return list(session.exec(select(DatasetBinding)
                             .order_by(DatasetBinding.dataset_id)))


# Site list and coordinator status


def latest_federation_list(session):
    return session.exec(select(FederationList)
                        .order_by(FederationList.version.desc())).first()


def store_federation_list(session, *, version, raw, signature, nodes, now=None):
    """Keep a signed site list, refusing one older than the newest stored.

    Going backwards is how a removed node would come back. Verifying the
    signature is the caller's job; this only keeps what passed.
    """
    latest = latest_federation_list(session)
    if latest is not None:
        if version < latest.version:
            raise StaleList(f"site list v{version} is older than "
                            f"v{latest.version}")
        if version == latest.version:
            if raw != latest.raw:
                raise Conflict(f"site list v{version} differs from the one stored")
            return latest
    row = FederationList(version=version, raw=raw, signature=signature,
                         nodes=nodes, received_at=now or utcnow())
    session.add(row)
    session.flush()
    return row


def record_poll(session, *, ok, error=None, now=None):
    now = now or utcnow()
    row = session.get(NodeStatus, 1)
    if row is None:
        row = NodeStatus(id=1)
        session.add(row)
    row.last_poll_at = now
    if ok:
        row.last_success_at = now
        row.last_error = None
    else:
        row.last_error = error
    session.flush()
    return row


def record_network_check(session, *, ok, detail, now=None):
    """The API process's check of the runner's network isolation, at start."""
    row = session.get(NodeStatus, 1)
    if row is None:
        row = NodeStatus(id=1)
        session.add(row)
    row.network_checked_at = now or utcnow()
    row.network_check_ok = ok
    row.network_check_detail = detail
    session.flush()
    return row


def node_status(session):
    return session.get(NodeStatus, 1)


# The TES API's own tables, which the Node screen only reads


def tes_listener(session):
    """The lease the TES API holds on this database, or None if none ever did."""
    return session.get(TesListener, 1)


def tes_task_counts(session):
    """State -> how many tasks are in it, for the states that have any."""
    rows = session.exec(select(TesTask.state, func.count()).group_by(TesTask.state))
    return {state: count for state, count in rows}


def latest_tes_tasks(session, limit=10):
    """The newest tasks. Only what a list needs: never a command or a log."""
    return list(session.exec(
        select(TesTask.id, TesTask.name, TesTask.owner, TesTask.image, TesTask.state,
               TesTask.created_at)
        .order_by(TesTask.created_at.desc(), TesTask.id).limit(limit)))
