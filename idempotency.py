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


def fingerprint(biochef_workflow: str, uploads) -> str:
    """What makes two submissions the same submission.

    The workflow and every uploaded byte. Length-prefixed rather than
    concatenated, because joining on a separator lets two different
    submissions hash the same -- a file named `a` holding `bc` against one
    named `ab` holding `c` -- and this value decides whether work is skipped.

    Sorted by name, so the order parts arrive in does not make the same
    submission look like a different one. Uploads are already read fully into
    memory by the caller, so this costs a hash of what is already there.
    """
    digest = hashlib.sha256()

    def chunk(value: bytes):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    chunk(biochef_workflow.encode())
    parts = sorted((name or "", body) for name, body in uploads)
    digest.update(len(parts).to_bytes(8, "big"))
    for name, body in parts:
        chunk(name.encode())
        chunk(body)
    return digest.hexdigest()
