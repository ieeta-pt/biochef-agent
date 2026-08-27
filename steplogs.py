"""What a run printed, and which step printed it (#6).

Snakemake writes one combined stream for the whole workflow, not one per rule.
So "per step" has two honest halves here, and it is worth being clear which is
which:

  attributed   a step that FAILED. Snakemake says so, in as many words --
               "Error in rule <name>:" followed by the block describing it --
               and the emitter derives every rule name from the node id. That
               mapping is exact and is what makes the answer to "which step
               broke" a fact rather than a guess.

  not split    a step that succeeded. Its output is in the run's stdout along
               with everything else's, and nothing in snakemake's output marks
               where one rule's writing ends and the next begins. Separating
               that needs a `log:` directive per rule, which is the emitter's
               business and a different piece of work.

Reporting the first and being plain about the second is more useful than
inventing a split by guessing at boundaries, which would be wrong exactly when
two steps fail and someone needs to know which said what.
"""

import os
import re
import threading
import time
from collections import deque

MAX_LOG_BYTES = int(os.getenv("BIOCHEF_MAX_LOG_BYTES", str(1024 * 1024)))
"""How much of a run's output is kept.

A tool that prints steadily can produce more than anyone wants held in memory,
and runs are held in memory. The TAIL is kept rather than the head: an error and
the traceback around it arrive at the end, and a truncated beginning costs
progress chatter.
"""

_ERROR_IN_RULE = re.compile(r"^Error in rule ([A-Za-z_][A-Za-z0-9_]*):", re.M)


def clamp(text, limit=None):
    """Keep the last `limit` bytes, and say so where it was cut.

    Marked rather than silently shortened. A log that begins mid-sentence with
    no explanation reads like a tool that produced nonsense.
    """
    limit = MAX_LOG_BYTES if limit is None else limit
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    marker = f"[... {len(text) - limit} earlier bytes dropped ...]\n"
    return marker + text[-limit:]


def failing_steps(stderr, node_ids, rule_name_for):
    """Which nodes snakemake blamed, and what it said about each.

    `rule_name_for` is passed in rather than imported so this module does not
    depend on the emitter; the caller supplies the one transform that exists.

    A rule name that maps to more than one node is reported against all of them
    with a note. Two node ids can collide -- "a.b" and "a-b" both become "a_b" --
    and quietly picking one would put a failure against a step that did not have
    it.
    """
    if not stderr:
        return {}

    by_rule = {}
    for node_id in node_ids:
        by_rule.setdefault(rule_name_for(node_id), []).append(node_id)

    blocks = {}
    matches = list(_ERROR_IN_RULE.finditer(stderr))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stderr)
        blocks.setdefault(match.group(1), []).append(stderr[match.start():end].strip())

    attributed = {}
    for rule, texts in blocks.items():
        owners = by_rule.get(rule)
        if not owners:
            # A rule this workflow did not produce -- "all", or something
            # snakemake generated. Not a node, so not attributable.
            continue
        ambiguous = len(owners) > 1
        for node_id in owners:
            attributed[node_id] = {
                "rule": rule,
                "stderr": "\n\n".join(texts),
            }
            if ambiguous:
                attributed[node_id]["ambiguous"] = sorted(owners)
    return attributed


# Snakemake announces each job as it starts and as it finishes.
#
# "Error in rule X:" contains "rule X:" as a substring, and mistaking it for a
# start would turn a node green in front of someone watching it break. What
# actually prevents that is .match(), which only ever matches at position zero;
# the leading ^ is belt and braces and no test can tell it from its absence.
# Kept because it still matters if anyone reaches for .search() later.
_RULE_STARTS = re.compile(r"^(?:local)?rule ([A-Za-z_][A-Za-z0-9_]*):\s*$")
_RULE_DONE = re.compile(r"^Finished jobid: \d+ \(Rule: ([A-Za-z_][A-Za-z0-9_]*)\)")
_RULE_FAILED = re.compile(r"^Error in rule ([A-Za-z_][A-Za-z0-9_]*):")

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETE = "COMPLETE"
FAILED = "FAILED"


class Progress:
    """Per-step status, built from snakemake's output as it arrives.

    The states are the four B4 asks for, spelled like the run states so the
    editor is not translating two vocabularies. A step nobody has mentioned is
    PENDING, which is the honest default: snakemake says nothing about a job
    until it starts one.

    FAILED is sticky. A rule that failed and is retried would otherwise report
    RUNNING again and lose the fact that it broke, and a node that has failed is
    the one thing a person watching wants to keep seeing.
    """

    def __init__(self, node_ids, rule_name_for):
        self._owners = {}
        for node_id in node_ids:
            self._owners.setdefault(rule_name_for(node_id), []).append(node_id)
        self._status = {node_id: PENDING for node_id in node_ids}

    def observe(self, line):
        """Take one line of output. Returns True if anything changed."""
        for pattern, state in ((_RULE_STARTS, RUNNING),
                               (_RULE_DONE, COMPLETE),
                               (_RULE_FAILED, FAILED)):
            match = pattern.match(line.rstrip("\n"))
            if not match:
                continue
            changed = False
            # Every node sharing the rule name, because "a.b" and "a-b" collide
            # and marking one of them would be a guess.
            for node_id in self._owners.get(match.group(1), ()):
                if self._status[node_id] == FAILED and state != FAILED:
                    continue
                if self._status[node_id] != state:
                    self._status[node_id] = state
                    changed = True
            return changed
        return False

    def snapshot(self):
        return dict(self._status)


class TailBuffer:
    """The last `max_bytes` of a stream, and how much was dropped to keep it.

    The tail rather than the head, because an error and the traceback around it
    arrive at the end; a truncated beginning costs progress chatter. And it says
    it was truncated, because a log that starts mid-sentence with no explanation
    reads like a tool that produced nonsense.

    One line longer than the whole budget is truncated to its own tail. That is
    not a contrived case: a tool emitting no newline -- a progress bar redrawing
    with \r, or binary on stdout -- arrives as a single line of whatever size,
    and a trim that stopped at the last line left the bound meaningless.

    Not thread-safe on its own. Callers that share one hold their own lock.
    """

    def __init__(self, max_bytes=None):
        self._lines = deque()
        self._bytes = 0
        self._dropped = 0
        self._max = MAX_LOG_BYTES if max_bytes is None else max_bytes

    def append(self, line):
        if len(line) > self._max:
            self._dropped += len(line) - self._max
            line = line[-self._max:]
        self._lines.append(line)
        self._bytes += len(line)
        while self._bytes > self._max and len(self._lines) > 1:
            oldest = self._lines.popleft()
            self._bytes -= len(oldest)
            self._dropped += len(oldest)

    def text(self):
        body = "".join(self._lines)
        if not self._dropped:
            return body
        return f"[... {self._dropped} earlier bytes dropped ...]\n" + body


class LiveLog:
    """Output accumulated as it arrives, delivered by one thread on a timer.

    The objection to streaming the logs was a lock per line, and it was a fair
    one: a chatty tool produces thousands, and the run store is shared with
    every poll. Delivering on a timer instead makes it a lock twice a second.

    Only the ticker delivers, and that is load-bearing rather than tidy. If a
    reader thread could deliver too, two of them could build snapshots in one
    order and hand them over in the other, and a client polling twice would see
    the log go backwards. One delivering thread cannot do that. It also keeps a
    slow consumer away from the readers draining the tool's pipes: if they
    stall the pipe fills and the tool stops writing.

    The buffer is bounded by the same MAX_LOG_BYTES the store keeps, so a run's
    output is not held twice over -- the runner has its own copy, and only a
    megabyte of it was ever going to be recorded. Bounding it bounds the cost
    of joining too, which would otherwise grow with the log.

    What this produces is a PARTIAL log. The authoritative one is recorded when
    the process exits, from the runner's complete capture, so anything trimmed
    or still buffered here costs nothing in the end.
    """

    def __init__(self, on_flush=None, every_seconds=0.5, max_bytes=None):
        self._lines = {"stdout": TailBuffer(max_bytes),
                       "stderr": TailBuffer(max_bytes)}
        self._lock = threading.Lock()
        self._on_flush = on_flush
        self._every_seconds = every_seconds
        self._pending = False
        self._stop = threading.Event()
        self._ticker = None

    def start(self):
        """Begin delivering. Idempotent, and a no-op with no callback."""
        if self._on_flush is None or self._ticker is not None:
            return self
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()
        return self

    def close(self):
        """Stop delivering, waiting for a delivery already in flight.

        The wait matters: perform_run closes this and then records the
        authoritative output, so a flush still running could otherwise deliver
        its partial snapshot afterwards -- overwriting a complete log with an
        incomplete one, which is worse than never having streamed.
        """
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=5)
            self._ticker = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.close()
        return False

    def add(self, stream, line):
        """Take a line. Appends and returns; it never delivers.

        Called from both reader threads, so it does as little as possible and
        holds the lock only long enough to append and trim.

        The two buckets are fixed rather than created on demand: there are
        exactly two streams, and a name that is not one of them is a caller
        error. It raises here rather than accumulating into something nothing
        will ever read.
        """
        with self._lock:
            self._lines[stream].append(line)
            self._pending = True

    def flush(self):
        """Deliver what has arrived, if anything has. Returns whether it did."""
        with self._lock:
            if not self._pending:
                return False
            self._pending = False
            snapshot = (self._lines["stdout"].text(),
                        self._lines["stderr"].text())
        if self._on_flush is not None:
            # Outside the lock, so a slow consumer cannot block a reader.
            try:
                self._on_flush(*snapshot)
            except Exception:                        # noqa: BLE001
                # The content is still buffered, so mark it undelivered again
                # rather than waiting for the next line to make it visible --
                # a tool that fell quiet right after a failed delivery would
                # otherwise show nothing more until the run ended.
                with self._lock:
                    self._pending = True
                raise
        return True

    def snapshot(self):
        """What is buffered now, as (stdout, stderr)."""
        with self._lock:
            return (self._lines["stdout"].text(),
                    self._lines["stderr"].text())

    def _tick(self):
        while not self._stop.wait(self._every_seconds):
            try:
                self.flush()
            except Exception:                        # noqa: BLE001
                # A failure in reporting must not end the thread and take the
                # rest of the run's logs with it.
                pass
