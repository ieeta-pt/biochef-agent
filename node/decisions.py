"""Signed decisions: what a node says, and proof that it said it.

A decision is built, reduced to canonical bytes, signed with the node key,
stored and written to the audit trail, in that order and inside the caller's
transaction. If the audit trail cannot be written the exception reaches the
caller and the transaction rolls back, so a decision never exists unrecorded.

The bytes are RFC 8785 (JSON Canonicalization Scheme). Whoever checks a signature
has to produce exactly the bytes that were signed, on any platform, in any
language, which ordinary json.dumps does not promise. Numbers are the one place
such schemes differ in practice, so a signed object may not contain a float.

The format is provisional: `biochef.node-decision.v1` has to be aligned with the
coordinator's agent API (docs/agent-ui/SPEC.md, section 7.2).
"""

import base64
import json
from datetime import datetime, timezone

import rfc8785
from cryptography.exceptions import InvalidSignature

import audit
from store import repo
from store.states import Action, SubjectType

SCHEMA = "biochef.node-decision.v1"

EVENT_DECISION = "node.decision"
EVENT_UNDONE = "node.decision.undone"


class DecisionError(Exception):
    """A decision that must not be made, or a signature that does not hold."""


def _refuse_floats(value, where="decision"):
    if isinstance(value, float):
        raise DecisionError(f"{where} is a float; signed objects hold none")
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_floats(item, f"{where}.{key}")
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_floats(item, f"{where}[{index}]")


def canonical(document):
    """The exact bytes a signature covers."""
    _refuse_floats(document)
    try:
        return rfc8785.dumps(document)
    except rfc8785.CanonicalizationError as failure:
        raise DecisionError(f"cannot be made canonical: {failure}") from None


def _timestamp(moment):
    if moment.tzinfo is None:
        raise DecisionError("a decision's time must say its zone")
    return moment.astimezone(timezone.utc)


def build(*, node, subject_type, subject_id, action, user, at, subject_hash=None,
          comment=None, dac_confirmed=None, reason=None):
    """The decision object of section 7.2.

    Approving a study version needs the data access committee's confirmation,
    and that field means nothing on any other decision, so it is refused there
    rather than signed and left for someone to misread.
    """
    if not user:
        raise DecisionError("a decision records who made it")
    is_study_approval = (subject_type is SubjectType.STUDY_VERSION
                         and action is Action.APPROVE)
    if is_study_approval and dac_confirmed is not True:
        raise DecisionError("approving a study version needs the data access "
                            "committee's confirmation")
    if not is_study_approval and dac_confirmed is not None:
        raise DecisionError("dac_confirmed belongs to study approvals only")

    document = {
        "schema": SCHEMA,
        "node": node,
        "subject_type": subject_type.value,
        "subject_id": str(subject_id),
        "subject_hash": subject_hash,
        "action": action.value,
        "user": user,
        "comment": comment,
        "at": _timestamp(at).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
    }
    if is_study_approval:
        document["dac_confirmed"] = True
    return document


def signature_of(data, keys):
    signature = keys.signing.sign(data)
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def sign(document, keys):
    """The canonical bytes of a document, and the signature over them."""
    data = canonical(document)
    return data, signature_of(data, keys)


def verify(object_raw, signature, public_key):
    """The decision a signature vouches for, or a refusal.

    Bytes that verify but are not canonical are refused too: they are not what
    this format signs, so something produced them that should not have.
    """
    try:
        padded = signature + "=" * (-len(signature) % 4)
        public_key.verify(base64.urlsafe_b64decode(padded), object_raw)
    except (InvalidSignature, ValueError):
        raise DecisionError("the signature does not verify") from None
    try:
        document = json.loads(object_raw)
    except ValueError:
        raise DecisionError("the signed bytes are not JSON") from None
    if canonical(document) != object_raw:
        raise DecisionError("the signed bytes are not canonical")
    if document.get("schema") != SCHEMA:
        raise DecisionError(f"unknown schema {document.get('schema')!r}")
    return document


def store(session, document, object_raw, signature):
    """Keep a signed decision. Its columns come from the object that was signed."""
    return repo.record_decision(
        session,
        subject_type=SubjectType(document["subject_type"]),
        subject_id=document["subject_id"],
        subject_hash=document["subject_hash"],
        action=Action(document["action"]),
        user=document["user"],
        comment=document["comment"],
        dac_confirmed=document.get("dac_confirmed"),
        reason=document["reason"],
        at=datetime.strptime(document["at"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc),
        object_raw=object_raw,
        signature=signature)


def _require_a_trail():
    # audit.record does nothing at all when no trail is configured, which is a
    # fair default for the API and not for a decision. A signed approval with no
    # audit event looks fine until someone needs the event.
    if audit.path() is None:
        raise DecisionError("decisions need an audit trail: set BIOCHEF_AUDIT_LOG")


def decide(session, keys, *, node, subject_type, subject_id, action, user,
           at=None, subject_hash=None, comment=None, dac_confirmed=None,
           reason=None):
    """Build, sign, store and audit one decision."""
    _require_a_trail()
    document = build(
        node=node, subject_type=subject_type, subject_id=subject_id,
        action=action, user=user, at=at or repo.utcnow(),
        subject_hash=subject_hash, comment=comment, dac_confirmed=dac_confirmed,
        reason=reason)
    object_raw, signature = sign(document, keys)
    row = store(session, document, object_raw, signature)
    audit.record(EVENT_DECISION, decision_id=row.id, node=node,
                 subject_type=row.subject_type, subject_id=row.subject_id,
                 subject_hash=row.subject_hash, action=row.action, user=row.user,
                 reason=row.reason, signed=True,
                 signed_by=keys.signing_fingerprint)
    return row


def undo(session, decision_id, *, user):
    """Take back a decision that has not left this node, and say who did.

    The decision goes; the trail keeps its event and gains this one, so that an
    undone decision is still visible to whoever reads the trail.
    """
    _require_a_trail()
    if not user:
        raise DecisionError("an undo records who made it")
    repo.delete_unposted_decision(session, decision_id)
    audit.record(EVENT_UNDONE, decision_id=decision_id, user=user)
