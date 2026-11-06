"""A request's life on this node: received, checked, recorded (section 6.3)."""

import json
import os

import audit
import passports
from node import decisions, verify as checks
from store import repo
from store.states import Action, SubjectType
from store.states import Verdict as Outcome

AGENT_USER = "agent"
"""Who a refusal is recorded under: the checks made it, not a person."""

EVENT_CHECKED = "node.request.checked"


class RequestError(Exception):
    """A document that is not a request at all."""


class VisaCheck:
    """Whether a member's visas allow the named datasets (check 9).

    A dataset needs a visa of the node's required type, from an issuer the node
    trusts, whose value is that dataset's id and whose subject is this member. The
    subject matters: a visa is a statement about a person, and one issued to
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

    def allow(self, tokens, ls_aai_id, datasets):
        if not self.required_type:
            return True
        if not isinstance(tokens, list) or ls_aai_id is None:
            return False
        accepted, _ = passports.verify_visas(
            [token for token in tokens if isinstance(token, str)],
            trusted_issuers=self.trusted_issuers, keyset_for=self.keyset_for)
        own = [visa for visa in accepted if visa.get("sub") == ls_aai_id]
        return all(passports.satisfies(own, self.required_type, dataset)
                   for dataset in datasets)


def receive(session, raw, now=None):
    """Keep a signed request as received, before any check."""
    try:
        document = json.loads(raw)
        job = document["job_request"]
        return repo.add_request(
            session, request_id=job["request_id"], study_id=job["study_id"],
            study_version=job["study_version"], study_digest=job["study_digest"],
            workflow_digest=job["workflow_digest"], member=job["member"],
            node=job["node"], datasets=job["datasets"],
            not_after=checks.parse_time(job["not_after"]),
            payload_digest=job["payload_digest"], raw=raw, now=now)
    except (ValueError, KeyError, TypeError, AttributeError) as failure:
        raise RequestError(f"not a signed request: {type(failure).__name__}") from None


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
    repo.set_verdict(session, request_id, result.outcome, failed_check=code,
                     joined_request_id=result.joined_request_id,
                     run_id=result.run_id)
    audit.record(EVENT_CHECKED, request_id=request_id, study_id=request.study_id,
                 study_version=request.study_version, member=request.member,
                 verdict=result.outcome.value, failed_check=code,
                 joined_request_id=result.joined_request_id)
    if result.outcome is Outcome.REFUSED:
        decisions.decide(session, keys, node=node, subject_type=SubjectType.REQUEST,
                         subject_id=request_id, action=Action.REFUSE,
                         user=AGENT_USER, at=now, reason=code)
    return result
