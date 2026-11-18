"""Signed requests in the shape of ieeta-pt/biochef-coordinator#6.

Built at test time, with keys generated at test time: no fixture holds a private
key, and the three details #6 leaves open are the ones node/verify.py names.
"""

import base64
import hashlib
import json
from datetime import timedelta

import pyrage
import rfc8785
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

WORKFLOW = {"nodes": [{"id": "qc", "type": "workflowNode",
                       "data": {"repo": "biochef/x@sha256:ab", "paramValues": {}}}],
            "edges": []}


def workflow_digest(workflow=None):
    data = rfc8785.dumps(WORKFLOW if workflow is None else workflow)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _b64url(number):
    return base64.urlsafe_b64encode(number.to_bytes(32, "big")).rstrip(b"=").decode()


class Member:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self):
        numbers = self.key.public_key().public_numbers()
        return {"kty": "EC", "crv": "P-256", "x": _b64url(numbers.x),
                "y": _b64url(numbers.y)}

    def sign(self, job):
        der = self.key.sign(rfc8785.dumps(job), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed_request(member, recipient, *, request_id="req_1", study_id="st",
                   study_version=1, study_digest="sha256:st", workflow=None,
                   digest=None, member_id="m-01", node="node-a",
                   datasets=("EGAD1",), not_after=None, visas=(), now=None,
                   plaintext=None, payload_digest=None, enc="age-x25519"):
    plain = plaintext if plaintext is not None else json.dumps(
        {"visas": list(visas), "workflow": WORKFLOW if workflow is None else workflow,
         "inputs": {}}).encode()
    ciphertext = pyrage.encrypt(plain, [pyrage.x25519.Recipient.from_str(recipient)])
    job = {"request_id": request_id, "study_id": study_id,
           "study_version": study_version, "study_digest": study_digest,
           "workflow_digest": digest or workflow_digest(),
           "member": member_id, "node": node, "datasets": list(datasets),
           "not_after": (not_after or now + timedelta(hours=1)).isoformat(),
           "payload_digest": payload_digest
           or "sha256:" + hashlib.sha256(plain).hexdigest()}
    return json.dumps({
        "job_request": job,
        "signature": {"alg": "ES256", "key": "m-01-key", "sig": member.sign(job)},
        "payload": {"enc": enc, "ciphertext": base64.b64encode(ciphertext).decode()},
    }).encode()
