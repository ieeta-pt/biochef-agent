"""Who executed what, over which data, when (#26, F7).

Append only, one JSON object per line, and deliberately not a log level. The
service's ordinary output is for whoever is debugging it; this is for whoever
has to answer, months later, what ran over a patient cohort and on whose
authority. Those two want different guarantees, and the second one does not
survive being mixed into stdout.

Three properties, and the reasons each is a property rather than a convention:

Append only. Written through a single os.write to a file opened O_APPEND, so
concurrent workers interleave whole lines rather than fragments, and so nothing
here can rewrite what it already said. This does not make the file immutable --
anything that can write it can truncate it -- and pretending otherwise would be
the dangerous kind of claim. Immutability is the filesystem's job: ship the file
somewhere append-only, or set the append-only attribute on it.

Exportable. JSON Lines, because the format has to be readable by something that
is not this service, years after this service stopped running. There is no
endpoint to fetch it. An audit trail reachable over the same API it audits is a
thing an attacker reads to find out what you noticed.

Honest about identity. `caller` records what the authentication provider
returned, which for the bearer provider is nothing at all -- a shared secret is
not an identity, and every holder is the same caller. The event records which
provider authorised the request so that "we do not know who" is legible as a
fact about the deployment rather than as a gap in the log. When a provider that
carries identity arrives (F3, Passports), the same field starts being useful
without the format changing.
"""

import json
import os
from datetime import datetime, timezone

SCHEMA = "biochef.audit-event.v1"

ENV_PATH = "BIOCHEF_AUDIT_LOG"


class AuditError(Exception):
    """The audit trail could not be written."""


def path():
    """Where events go, or None when no trail is configured.

    Unset means disabled, which is the existing behaviour and stays the default:
    turning this on is a deployment decision, and a service that started writing
    to a path nobody chose would be worse than one that wrote nowhere.
    """
    value = (os.getenv(ENV_PATH) or "").strip()
    return value or None


def record(event, **fields):
    """Append one event. Returns the line written, or None when disabled.

    Failing to write the trail raises. A TRE that believes it is auditing and is
    not is in a worse position than one that knows it is not auditing, so this
    does not swallow errors -- the caller decides whether the work may proceed
    without a record of it.
    """
    destination = path()
    if destination is None:
        return None

    entry = {
        "schema": SCHEMA,
        "event": event,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    # Sorted so two events with the same content produce the same bytes, which
    # is what makes an exported trail diffable and checkable.
    for key in sorted(fields):
        entry[key] = fields[key]

    # separators without spaces, and one \n, so a line is exactly one event.
    line = json.dumps(entry, sort_keys=False, separators=(",", ":")) + "\n"

    try:
        # O_APPEND rather than seek-to-end: the position is chosen by the kernel
        # at write time, so two workers cannot land on the same offset. One
        # os.write per event, because a line split across two calls is a line
        # another writer can interleave into.
        handle = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, line.encode("utf-8"))
        finally:
            os.close(handle)
    except OSError as exc:
        raise AuditError(f"the audit trail at {destination} could not be written: {exc}") from exc
    return line


def read(destination=None):
    """Every event in the trail, for tests and for whoever exports it.

    A malformed line is returned as an error record rather than skipped or
    raised on. A trail that silently drops what it cannot parse is one you
    cannot reason about, and one bad line must not make the rest unreadable.
    """
    destination = destination or path()
    if not destination or not os.path.exists(destination):
        return []
    entries = []
    with open(destination, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError as exc:
                entries.append({"schema": SCHEMA, "event": "unreadable",
                                "line": number, "error": str(exc)})
    return entries
