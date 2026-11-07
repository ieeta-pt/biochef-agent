"""A request's life on this node: received, checked, recorded (section 6.3)."""

import json
import os

import audit
import passports
from node import decisions, verify as checks
from store import repo
from node.verify import Outcome
from store.states import Action, RefusalCode, SubjectType, Verdict

AGENT_USER = "agent"
"""Who a refusal is recorded under: the checks made it, not a person."""

EVENT_CHECKED = "node.request.checked"


class RequestError(Exception):
    """A document that is not a request at all."""


class VisaCheck:
    """Whether a member's visas allow the named datasets (check 9).

    A dataset needs a visa of the node's required type, from an issuer the node
    trusts, whose value is exactly the visa value of the dataset's binding and
    whose subject is this member. A binding with no visa value matches no visa.
    The subject matters: a visa is a statement about a person, and one issued to
    somebody else proves nothing about this one.
    """

    def __init__(self, required_type, trusted_issuers=(), keyset_for=None):
        if required_type and not trusted_issuers:
            raise ValueError("BIOCHEF_PASSPORT_REQUIRE_VISA is set but "
                             "BIOCHEF_PASSPORT_VISA_ISSUERS is empty")
        self.required_type = required_type
        self.trusted_issuers = frozenset(trusted_issuers)
        self.keyset_for = keyset_for or self._keyset
        self._keysets = {}

    @classmethod
    def from_settings(cls):
        issuers = os.getenv("BIOCHEF_PASSPORT_VISA_ISSUERS") or ""
        return cls((os.getenv("BIOCHEF_PASSPORT_REQUIRE_VISA") or "").strip() or None,
                   [item.strip() for item in issuers.split(",") if item.strip()])

    def _keyset(self, issuer):
        if issuer not in self._keysets:
            self._keysets[issuer] = passports.KeySet(passports.jwks_url_for(issuer))
        return self._keysets[issuer]

    def allow(self, tokens, ls_aai_id, values):
        """`values`: the visa value of each dataset, None for one that has none."""
        if not self.required_type:
            return True
        if not isinstance(tokens, list) or ls_aai_id is None or None in values:
            return False
        accepted, _ = passports.verify_visas(
            [token for token in tokens if isinstance(token, str)],
            trusted_issuers=self.trusted_issuers, keyset_for=self.keyset_for)
        own = [visa for visa in accepted if visa.get("sub") == ls_aai_id]
        return all(passports.satisfies(own, self.required_type, value)
                   for value in values)


def _fields(raw):
    try:
        job = json.loads(raw)["job_request"]
        fields = {
            "request_id": job["request_id"], "study_id": job["study_id"],
            "study_version": job["study_version"], "study_digest": job["study_digest"],
            "workflow_digest": job["workflow_digest"], "member": job["member"],
            "node": job["node"], "datasets": job["datasets"],
            "not_after": checks.parse_time(job["not_after"]),
            "payload_digest": job["payload_digest"]}
    except (ValueError, KeyError, TypeError, AttributeError) as failure:
        raise RequestError(f"not a signed request: {type(failure).__name__}") from None
    if not isinstance(fields["request_id"], str) or not fields["request_id"]:
        raise RequestError("not a signed request: no request_id")
    return fields


def receive(session, raw, now=None):
    """Keep a signed request as received, before any check."""
    return repo.add_request(session, **_fields(raw), raw=raw, now=now)


def deliver(session, keys, raw, *, node, visas, now=None):
    """Everything one delivery from the coordinator gets. Returns the Result.

    A new request_id is received and checked. One already here is a repeat: its
    stored verdict stays as it is, and the delivery gets what check 6 says.
    """
    now = now or repo.utcnow()
    fields = _fields(raw)
    seen = repo.find_request(session, fields["request_id"])
    if seen is None:
        receive(session, raw, now=now)
        return check(session, keys, fields["request_id"], node=node, visas=visas,
                     now=now)
    if seen.verdict is None:
        # Received and never checked, as when the process stopped in between.
        check(session, keys, seen.request_id, node=node, visas=visas, now=now)
    return repeat(session, keys, raw, node=node, visas=visas, now=now)


def repeat(session, keys, raw, *, node, visas, now=None):
    """A delivery of a request_id already checked here.

    Only the same signed request counts as a repeat. Anything else under the id is
    not stored: a replay is refused with a signed decision, since the member did
    sign it, and a delivery that fails an earlier check is only audited.
    """
    now = now or repo.utcnow()
    decisions.require_a_trail()
    delivery = repo.request_as_delivered(**_fields(raw), raw=raw, now=now)
    result = checks.verify(delivery, session, keys, now, node=node, visas=visas)
    code = result.failed_check.value if result.failed_check else None
    if result.repeat_of is not None:
        repo.note_repeat(session, delivery.request_id, now)
    audit.record(EVENT_CHECKED, request_id=delivery.request_id,
                 study_id=delivery.study_id, study_version=delivery.study_version,
                 member=delivery.member, verdict=result.outcome.value,
                 failed_check=code, repeat=True,
                 repeat_of=result.repeat_of)
    if result.failed_check is RefusalCode.REPLAY:
        decisions.decide(session, keys, node=node, subject_type=SubjectType.REQUEST,
                         subject_id=delivery.request_id, action=Action.REFUSE,
                         user=AGENT_USER, at=now, reason=code)
    return result


def check(session, keys, request_id, *, node, visas, now=None):
    """Check a request and record the verdict, once. Returns the Result, or None.

    An accepted payload comes back to the caller and is stored nowhere, because it
    holds the member's visas and the run's parameters.
    """
    now = now or repo.utcnow()
    decisions.require_a_trail()
    request = repo.get_request(session, request_id)
    if request.verdict is not None:
        return None
    result = checks.verify(request, session, keys, now, node=node, visas=visas)
    code = result.failed_check.value if result.failed_check else None
    repo.set_verdict(session, request_id, Verdict(result.outcome.value),
                     failed_check=code, run_id=result.run_id)
    audit.record(EVENT_CHECKED, request_id=request_id, study_id=request.study_id,
                 study_version=request.study_version, member=request.member,
                 verdict=result.outcome.value, failed_check=code)
    if result.outcome is Outcome.REFUSED:
        decisions.decide(session, keys, node=node, subject_type=SubjectType.REQUEST,
                         subject_id=request_id, action=Action.REFUSE,
                         user=AGENT_USER, at=now, reason=code)
    return result
