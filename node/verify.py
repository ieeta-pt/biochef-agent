"""The ten checks of a request, in the order of section 6.3.

`verify` reads and decides, and changes nothing: recording the result is
node/requests.py. The first failure stops the rest, so what it returns says how
far a request got.

The agent API of the coordinator does not exist yet. These follow the models in
ieeta-pt/biochef-coordinator#6, which fix the fields but not three details, and
each is an assumption here until the API settles it:

- the member signs the RFC 8785 form of `job_request`, and `sig` is the raw r and
  s in base64url;
- `payload_digest` is over the decrypted bytes;
- the payload is JSON with `visas`, a list of JWTs, and `workflow`, whose digest
  is over its RFC 8785 form.
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pyrage
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from node import age, decisions
from store import repo
from store.states import RefusalCode, VersionState
from store.states import Verdict as Outcome


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    failed_check: Optional[RefusalCode] = None
    joined_request_id: Optional[str] = None
    run_id: Optional[str] = None
    payload: Any = None
    """Only on acceptance, in memory, for starting the run. Never stored."""


def _refused(code):
    return Result(Outcome.REFUSED, failed_check=code)


def _document(request):
    try:
        document = json.loads(request.raw)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _b64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_key(jwk):
    x = int.from_bytes(_b64url(jwk["x"]), "big")
    y = int.from_bytes(_b64url(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def _signed_by_member(document, request, session):
    """Check 1. A version this node does not hold cannot vouch for anyone."""
    version = repo.study_version_named(session, request.study_id,
                                       request.study_version)
    if version is None or document is None:
        return False
    key = next((row for row in repo.member_keys(session, version.id)
                if row.member == request.member), None)
    signature = document.get("signature")
    if key is None or not isinstance(signature, dict):
        return False
    if signature.get("alg") != "ES256":
        return False
    try:
        raw = _b64url(signature["sig"])
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                   int.from_bytes(raw[32:], "big"))
        _public_key(key.signing_key).verify(
            der, decisions.canonical(document["job_request"]),
            ec.ECDSA(hashes.SHA256()))
    except (KeyError, TypeError, ValueError, binascii.Error, InvalidSignature,
            AttributeError, decisions.DecisionError):
        return False
    return True


def _study_approved(request, session):
    version = repo.study_version_named(session, request.study_id,
                                       request.study_version)
    return (version.state == VersionState.APPROVED.value
            and version.digest == request.study_digest)


def _workflow_approved(request, session):
    version = repo.study_version_named(session, request.study_id,
                                       request.study_version)
    return repo.approved_workflow(session, version.id,
                                  request.workflow_digest) is not None


def _decrypted(document, request, keys):
    """Check 7: the payload as an object, or None."""
    try:
        payload = document["payload"]
        if payload["enc"] != "age-x25519":
            return None
        plain = age.decrypt(keys, base64.b64decode(payload["ciphertext"]))
    except (KeyError, TypeError, ValueError, binascii.Error, pyrage.DecryptError,
            pyrage.IdentityError):
        return None
    if "sha256:" + hashlib.sha256(plain).hexdigest() != request.payload_digest:
        return None
    try:
        decoded = json.loads(plain)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _workflow_digest(payload):
    """Check 8's recomputation. None when the workflow cannot be put in canonical form."""
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        return None
    try:
        return "sha256:" + hashlib.sha256(decisions.canonical(workflow)).hexdigest()
    except decisions.DecisionError:
        return None


def _member_id(request, session):
    version = repo.study_version_named(session, request.study_id,
                                       request.study_version)
    for row in repo.member_keys(session, version.id):
        if row.member == request.member:
            return row.ls_aai_id
    return None


def verify(request, store, keys, now, *, node, visas):
    """The verdict on one request. `store` is a session, and is only read."""
    session = store
    document = _document(request)

    if not _signed_by_member(document, request, session):
        return _refused(RefusalCode.BAD_SIGNATURE)
    if request.node != node:
        return _refused(RefusalCode.WRONG_NODE)
    if not _study_approved(request, session):
        return _refused(RefusalCode.STUDY_NOT_APPROVED)
    if not _workflow_approved(request, session):
        return _refused(RefusalCode.WORKFLOW_NOT_APPROVED)
    if request.not_after <= now:
        return _refused(RefusalCode.EXPIRED)

    earlier = repo.first_with_payload(session, request.payload_digest,
                                      excluding=request.request_id)
    if earlier is not None:
        if earlier.verdict == Outcome.REFUSED.value:
            return _refused(RefusalCode.REPLAY)
        # The earliest one, so never itself a join.
        return Result(Outcome.JOINED, joined_request_id=earlier.request_id,
                      run_id=earlier.run_id)

    payload = _decrypted(document, request, keys)
    if payload is None:
        return _refused(RefusalCode.CANNOT_DECRYPT)
    if _workflow_digest(payload) != request.workflow_digest:
        return _refused(RefusalCode.HASH_MISMATCH)
    if not visas.allow(payload.get("visas"), _member_id(request, session),
                       request.datasets):
        return _refused(RefusalCode.VISA_MISSING)
    if any(repo.dataset_binding(session, dataset) is None
           for dataset in request.datasets):
        return _refused(RefusalCode.DATASET_NOT_BOUND)
    return Result(Outcome.ACCEPTED, payload=payload)


def parse_time(text):
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a time must say its zone")
    return moment.astimezone(timezone.utc)
