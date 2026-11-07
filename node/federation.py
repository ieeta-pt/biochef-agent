"""The list of nodes in the federation, and who vouches for it (section 7.4).

A study names nodes, and the Inbox says whether each is on this list, so a forged
list would let a study reach somewhere nobody approved. The list is taken only
with a valid signature, and never older than the newest one here: going backwards
is how a removed node comes back.

The coordinator's agent API does not exist yet, and #6 does not define the list.
Until it does this assumes an Ed25519 signature, base64url, over the bytes of the
document as received, a PEM public key in BIOCHEF_FEDERATION_KEY, and a document
with `version` and `nodes`.
"""

import base64
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from store import repo


class FederationError(Exception):
    """A list or a key that cannot be accepted."""


def load_key(path=None):
    """The public key that signs the node list."""
    path = path or (os.getenv("BIOCHEF_FEDERATION_KEY") or "").strip()
    if not path:
        raise FederationError("BIOCHEF_FEDERATION_KEY is not set")
    try:
        with open(path, "rb") as handle:
            key = serialization.load_pem_public_key(handle.read())
    except (OSError, ValueError) as failure:
        raise FederationError(f"BIOCHEF_FEDERATION_KEY: {failure}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise FederationError("BIOCHEF_FEDERATION_KEY is not an Ed25519 public key")
    return key


def check(raw, signature, key):
    """The document, if the signature holds and it has the right shape."""
    try:
        padded = signature + "=" * (-len(signature) % 4)
        key.verify(base64.urlsafe_b64decode(padded), raw)
    except (InvalidSignature, ValueError, TypeError):
        raise FederationError("the node list's signature does not verify") from None
    try:
        document = json.loads(raw)
        version, nodes = document["version"], document["nodes"]
    except (ValueError, KeyError, TypeError):
        raise FederationError("the node list is not a list of nodes") from None
    if (not isinstance(version, int) or isinstance(version, bool)
            or not isinstance(nodes, list)):
        raise FederationError("the node list is not a list of nodes")
    return document


def accept(session, raw, signature, key, now=None):
    """Keep a verified list. An older version raises StaleList from the store."""
    document = check(raw, signature, key)
    return repo.store_federation_list(session, version=document["version"], raw=raw,
                                      signature=signature, nodes=document["nodes"],
                                      now=now)
