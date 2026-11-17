"""Recognising a submission this agent has already accepted (#87).

A hub retries when a submission times out, when a proxy drops the response, or
when it restarts mid-flight and replays its queue. Each retry currently starts
the work again, takes another execution slot and retains another record, so
retrying under load pushes the agent further into the 503 that caused it.

An `Idempotency-Key` fixes that: a key not seen before creates a run, a key
already seen returns the run it created.

Two decisions here are deliberately the opposite of the ones made for
`X-Request-Id` in #84, and the asymmetry is the point.

**An unusable key is refused, not replaced.** There, ignoring a bad header
costs a lost correlation, and refusing would have destroyed a valid
submission, so a bad value is silently replaced. Here, ignoring a bad key costs
the caller the exact protection it asked for: it believes a retry is safe,
retries, and the work runs twice. Silently degrading a safety guarantee is
worse than refusing the request outright.

**The key is not echoed in a header.** It identifies a submission rather than
describing a response, and putting it on the way out would make it a second
thing to keep legal for the wire. It appears on the run record, where a caller
asked for it.
"""

import hashlib
import re

HEADER = "idempotency-key"

MAX_LENGTH = 255
"""What the de-facto implementations of this header allow.

One copy is kept per retained run, so at the default MAX_RUNS this bounds the
index at about 64 KiB. Long enough for any key a client composes from its own
identifiers, which is the point of letting the client choose it.
"""

ACCEPTABLE = re.compile(r"\A[A-Za-z0-9._~:/+=@-]{1,%d}\Z" % MAX_LENGTH)
"""Deliberately the same character set as a request id, for one less rule.

Unlike a request id this is never written to a header, so the constraint is not
about what a server will transmit -- it is about a key being a stable, opaque
token a client can reproduce exactly on a retry. A value that needs encoding,
trimming or normalising somewhere along the way is not that.
"""


class MalformedKey(ValueError):
    """The key cannot be used, so the request is refused rather than run.

    Accepting the submission while quietly dropping the key would hand back a
    202 that looks like protection and is not.

    The other two outcomes -- a key already used, and a key still being
    admitted -- are raised by RunStore, because only the store can know them
    and only it can decide them atomically. They are not duplicated here.
    """


def validated(offered):
    """The key to use, or None when the caller offered none.

    Raises MalformedKey for anything else. See the module docstring for why
    this refuses where the request id replaces.
    """
    if offered is None:
        return None
    if not ACCEPTABLE.match(offered):
        raise MalformedKey(
            f"Idempotency-Key must be 1 to {MAX_LENGTH} characters of "
            f"A-Za-z0-9._~:/+=@- ; the request was not run, so it is safe to "
            f"retry with a usable key"
        )
    return offered


class Fingerprint:
    """What makes two submissions the same submission, built as it arrives.

    The workflow and every uploaded byte. The uploads are streamed to disk in
    chunks rather than held in memory, so the hash is fed the same chunks on
    the way past instead of reading anything back.

    Each part is hashed on its own: its name length-prefixed, then its bytes,
    then their length. Joining on a separator would let two different
    submissions hash the same -- a file named `a` holding `bc` against one
    named `ab` holding `c` -- and this value decides whether work is skipped.
    The length goes after the bytes because it is only known once they have
    all passed; with the name's length fixed in front, the last eight bytes
    are unambiguous.

    The part digests are sorted before they are combined, so the order parts
    arrive in does not make the same submission look like a different one.
    """

    def __init__(self, biochef_workflow: str):
        self._workflow = biochef_workflow.encode()
        self._parts = []

    def part(self, name):
        """Start the next upload; feed its bytes to the returned hash."""
        part = _Part(name or "")
        self._parts.append(part)
        return part

    def hexdigest(self) -> str:
        digest = hashlib.sha256()
        digest.update(len(self._workflow).to_bytes(8, "big"))
        digest.update(self._workflow)
        digest.update(len(self._parts).to_bytes(8, "big"))
        for value in sorted(part.digest() for part in self._parts):
            digest.update(value)
        return digest.hexdigest()


class _Part:
    def __init__(self, name: str):
        encoded = name.encode()
        self._hash = hashlib.sha256()
        self._hash.update(len(encoded).to_bytes(8, "big"))
        self._hash.update(encoded)
        self._length = 0

    def update(self, chunk: bytes) -> None:
        self._hash.update(chunk)
        self._length += len(chunk)

    def digest(self) -> bytes:
        final = self._hash.copy()
        final.update(self._length.to_bytes(8, "big"))
        return final.digest()


def fingerprint(biochef_workflow: str, uploads) -> str:
    """Fingerprint of (name, bytes) pairs already in memory."""
    built = Fingerprint(biochef_workflow)
    for name, body in uploads:
        built.part(name).update(body)
    return built.hexdigest()
