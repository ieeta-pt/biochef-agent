"""What a data manager decides: the state change and its signed decision together.

Each function takes the caller's transaction, so that the states and the
decisions land together or not at all. That is the point of rule 6.2.1: approving
v3 moves v2 to superseded and writes two signed decisions, and no failure between
those steps can leave two valid versions or an approval nobody signed.

Undo is here too, because it is more than deleting a row. A decision moved
states, and taking it back has to put them where they were, including whatever
an approval superseded. It is refused once anything could have relied on the
decision: when it has left this node, when a later decision followed it, or when
a request already ran under it.
"""

from store import repo
from store.errors import AlreadyPosted, Conflict
from store.states import Action, SubjectType, VersionState

from node import decisions

SUPERSEDED = "superseded"
"""The reason on the withdrawal an approval writes for the version it replaces."""

_REVERSALS = {
    Action.APPROVE: (VersionState.APPROVED, VersionState.PENDING),
    Action.REFUSE: (VersionState.REFUSED, VersionState.PENDING),
    Action.WITHDRAW: (VersionState.WITHDRAWN, VersionState.APPROVED),
}


def _study_decision(session, keys, node, version, action, user, now, **fields):
    return decisions.decide(
        session, keys, node=node, subject_type=SubjectType.STUDY_VERSION,
        subject_id=version.id, subject_hash=version.digest, action=action,
        user=user, at=now, **fields)


def _workflow_decision(session, keys, node, workflow, action, user, now, **fields):
    return decisions.decide(
        session, keys, node=node, subject_type=SubjectType.WORKFLOW_VERSION,
        subject_id=workflow.id, subject_hash=workflow.digest, action=action,
        user=user, at=now, **fields)


def approve_study(session, keys, *, node, version_id, user, dac_confirmed,
                  comment=None, now=None):
    """Approve a version and supersede the one before, as one decision of the user.

    The withdrawal carries the same user: it happened because of what they did,
    and every decision names a person.
    """
    now = now or repo.utcnow()
    replaced = repo.approve_study_version(session, version_id, now=now)
    version = repo.get_study_version(session, version_id)
    approval = _study_decision(session, keys, node, version, Action.APPROVE, user,
                               now, dac_confirmed=dac_confirmed or None,
                               comment=comment)
    if replaced is not None:
        _study_decision(session, keys, node, replaced, Action.WITHDRAW, user, now,
                        reason=SUPERSEDED)
    return approval


def refuse_study(session, keys, *, node, version_id, user, comment=None, now=None):
    now = now or repo.utcnow()
    version = repo.refuse_study_version(session, version_id, now=now)
    return _study_decision(session, keys, node, version, Action.REFUSE, user, now,
                           comment=comment)


def withdraw_study(session, keys, *, node, version_id, user, comment=None, now=None):
    now = now or repo.utcnow()
    version = repo.withdraw_study_version(session, version_id, now=now)
    return _study_decision(session, keys, node, version, Action.WITHDRAW, user, now,
                           comment=comment)


def approve_workflow(session, keys, *, node, workflow_id, user, comment=None,
                     now=None):
    now = now or repo.utcnow()
    workflow = repo.approve_workflow_version(session, workflow_id, now=now)
    return _workflow_decision(session, keys, node, workflow, Action.APPROVE, user,
                              now, comment=comment)


def refuse_workflow(session, keys, *, node, workflow_id, user, comment=None,
                    now=None):
    now = now or repo.utcnow()
    workflow = repo.refuse_workflow_version(session, workflow_id, now=now)
    return _workflow_decision(session, keys, node, workflow, Action.REFUSE, user,
                              now, comment=comment)


def withdraw_workflow(session, keys, *, node, workflow_id, user, comment=None,
                      now=None):
    now = now or repo.utcnow()
    workflow = repo.withdraw_workflow_version(session, workflow_id, now=now)
    return _workflow_decision(session, keys, node, workflow, Action.WITHDRAW, user,
                              now, comment=comment)


def _subject(session, decision):
    if decision.subject_type == SubjectType.STUDY_VERSION.value:
        return repo.get_study_version(session, int(decision.subject_id))
    if decision.subject_type == SubjectType.WORKFLOW_VERSION.value:
        return repo.get_workflow_version(session, int(decision.subject_id))
    return None


def _withdrawal_of(session, replaced):
    for row in repo.decisions_about(session, SubjectType.STUDY_VERSION, replaced.id):
        if row.action == Action.WITHDRAW.value and row.reason == SUPERSEDED:
            return row
    raise Conflict(f"study version {replaced.id} has no recorded withdrawal")


def _study_version_of(session, subject):
    if subject.__tablename__ == "study_version":
        return subject
    return repo.get_study_version(session, subject.study_version_id)


def _refusal(session, decision, subject):
    """Why this decision cannot be undone, or None."""
    if subject is None:
        return "this kind of decision cannot be undone here"
    action = Action(decision.action)
    if action not in _REVERSALS:
        return "this kind of decision cannot be undone here"
    if repo.has_left(decision):
        return "it has already been sent to the coordinator"

    subject_type = SubjectType(decision.subject_type)
    latest = repo.decisions_about(session, subject_type, subject.id)[0]
    if latest.id != decision.id:
        return "a later decision followed it"
    made, restored = _REVERSALS[action]
    if subject.state != made.value:
        # A withdrawal written by an approval lands here: the version is
        # superseded, and undoing the approval is what takes it back.
        return "this version has moved on since"

    study_version = _study_version_of(session, subject)
    is_study = subject_type is SubjectType.STUDY_VERSION
    digest = None if is_study else subject.digest
    if (action is Action.APPROVE and repo.count_requests_run_under(
            session, study_version.study_id, study_version.version, digest)):
        return "requests have already run under it"

    if is_study and action is Action.APPROVE:
        if any(row.state != VersionState.PENDING.value
               for row in repo.workflow_versions(session, subject.id)):
            return "workflows have been decided under this version"
        replaced = repo.versions_replaced_at(session, subject)
        if (replaced is not None
                and repo.has_left(_withdrawal_of(session, replaced))):
            return "the withdrawal it caused has already been sent"
    if restored is VersionState.APPROVED:
        if is_study:
            if repo.approved_version(session, subject.study_id) is not None:
                return "another version of this study is approved now"
        elif study_version.state != VersionState.APPROVED.value:
            return "its study version is no longer approved"
    elif is_study:
        # Back to pending under a newer approved version, it could never be
        # approved: it would wait in the Inbox for ever.
        current = repo.approved_version(session, subject.study_id)
        if current is not None and current.version > subject.version:
            return f"v{current.version} is approved now"
    elif study_version.state not in (VersionState.PENDING.value,
                                     VersionState.APPROVED.value):
        return "its study version is no longer open"
    return None


def undo_refusal(session, decision):
    """Why a decision cannot be undone, or None. The page asks this to offer it."""
    return _refusal(session, decision, _subject(session, decision))


def _restore_replaced(session, version, replaced, now):
    # Read before the version moves: the match is on the moment they changed.
    moved = [row for row in repo.workflow_versions(session, replaced.id)
             if row.state == VersionState.SUPERSEDED.value
             and row.state_changed_at == version.state_changed_at]
    # Out of the way first: the index allows one approved row at any instant.
    repo.restore_version(session, version, VersionState.PENDING, now)
    repo.restore_version(session, replaced, VersionState.APPROVED, now)
    for workflow in moved:
        about = repo.decisions_about(session, SubjectType.WORKFLOW_VERSION, workflow.id)
        was_approved = bool(about) and about[0].action == Action.APPROVE.value
        repo.restore_version(
            session, workflow,
            VersionState.APPROVED if was_approved else VersionState.PENDING, now)


def _withdrawn_with(session, version):
    """The workflows a study version's withdrawal took with it: withdrawn at
    the same moment, and not by a withdrawal of their own."""
    moved = []
    for row in repo.workflow_versions(session, version.id):
        if (row.state != VersionState.WITHDRAWN.value
                or row.state_changed_at != version.state_changed_at):
            continue
        about = repo.decisions_about(session, SubjectType.WORKFLOW_VERSION, row.id)
        if about and about[0].action == Action.WITHDRAW.value:
            continue
        moved.append((row, bool(about) and about[0].action == Action.APPROVE.value))
    return moved


def _reopen_waiting(session, study_id, now):
    """Put back to pending the versions an undone approval superseded while
    they waited, with their workflows.

    Found by what they are rather than by when they moved, because one may have
    arrived after the approval and been stored superseded: no decision was ever
    made about it here, and it is newer than whatever is approved now.
    """
    current = repo.approved_version(session, study_id)
    for row in repo.study_versions(session, study_id):
        if (row.state != VersionState.SUPERSEDED.value
                or repo.decisions_about(session, SubjectType.STUDY_VERSION, row.id)
                or (current is not None and row.version <= current.version)):
            continue
        repo.restore_version(session, row, VersionState.PENDING, now)
        for workflow in repo.workflow_versions(session, row.id):
            if workflow.state == VersionState.SUPERSEDED.value:
                repo.restore_version(session, workflow, VersionState.PENDING, now)


def undo(session, decision_id, *, user, now=None):
    """Take a decision back, with everything it moved, and say who did."""
    now = now or repo.utcnow()
    decision = repo.get_decision(session, decision_id)
    subject = _subject(session, decision)
    reason = _refusal(session, decision, subject)
    if reason is not None:
        if repo.has_left(decision):
            raise AlreadyPosted(f"decision {decision_id}: {reason}")
        raise Conflict(f"This decision cannot be undone: {reason}.")

    action = Action(decision.action)
    made, restored = _REVERSALS[action]
    is_study = decision.subject_type == SubjectType.STUDY_VERSION.value
    replaced = None
    if is_study and action is Action.APPROVE:
        replaced = repo.versions_replaced_at(session, subject)
    withdrawn_with = (_withdrawn_with(session, subject)
                      if is_study and action is Action.WITHDRAW else [])

    if replaced is not None:
        withdrawal = _withdrawal_of(session, replaced)
        _restore_replaced(session, subject, replaced, now)
        decisions.undo(session, withdrawal.id, user=user)
    else:
        repo.restore_version(session, subject, restored, now)
    for workflow, was_approved in withdrawn_with:
        repo.restore_version(
            session, workflow,
            VersionState.APPROVED if was_approved else VersionState.PENDING, now)
    if is_study and action is Action.APPROVE:
        _reopen_waiting(session, subject.study_id, now)
    decisions.undo(session, decision.id, user=user)
    return subject
