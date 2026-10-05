"""Run-level diagnostics and separate rule output (#6).

For asynchronous runs the emitter directs each rule's diagnostic stdout and
stderr to separate files. Snakemake's own messages remain in the run-level
stream. Its error headings are diagnostic hints, not step-output boundaries:

  attributed   on a failed workflow, stderr blocks headed "Error in rule
               <name>:" are mapped to node ids using the emitter's rule names.
               This is diagnostic text, not verified step identity; use the
               separate per-rule files for the output a rule printed.

The per-rule files are the source of `node_logs`. Scientific stdout already
redirected into a declared output file stays in that output, not in a log.
"""

import os
import re
import uuid

MAX_LOG_BYTES = int(os.getenv("BIOCHEF_MAX_LOG_BYTES", str(1024 * 1024)))
"""How much output is retained after a run.

The TAIL is kept rather than the head: an error and the traceback around it
often arrive at the end. This does not bound the runner's peak capture or the
size of temporary per-rule log files while tools are still running.
"""

if MAX_LOG_BYTES < 1:
    raise ValueError("BIOCHEF_MAX_LOG_BYTES must be positive")

_ERROR_IN_RULE = re.compile(r"^Error in rule ([A-Za-z_][A-Za-z0-9_]*):", re.M)


def clamp(text, limit=None):
    """Keep the last `limit` bytes, and say so where it was cut.

    Marked rather than silently shortened. A log that begins mid-sentence with
    no explanation reads like a tool that produced nonsense.
    """
    limit = MAX_LOG_BYTES if limit is None else limit
    if limit < 1:
        raise ValueError("log limit must be positive")
    if text is None:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # A byte boundary can land inside a UTF-8 character. Drop that partial
    # character and count its bytes among those omitted.
    tail = encoded[-limit:].decode("utf-8", errors="ignore")
    dropped = len(encoded) - len(tail.encode("utf-8"))
    return f"[... {dropped} earlier bytes dropped ...]\n" + tail


def make_step_log_names(count):
    """Use opaque, plain workspace names for one stdout/stderr pair per rule."""
    prefix = f"bclog-{uuid.uuid4().hex}"
    return [(f"{prefix}-{index}.stdout", f"{prefix}-{index}.stderr")
            for index in range(count)]


def _read_log_tail(ws, name, limit):
    try:
        with ws.open_read(name, regular_only=True) as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - limit))
            raw = log.read(limit)
    except FileNotFoundError:
        return None

    text = raw.decode("utf-8", errors="ignore")
    omitted = size - len(text.encode("utf-8"))
    if omitted:
        return f"[... {omitted} bytes omitted ...]\n" + text
    return text


def read_node_logs(ws, node_ids, step_logs):
    """Read per-rule files through the workspace, within one run-wide budget."""
    if len(node_ids) != len(step_logs):
        raise ValueError("one pair of log names is required for each node")
    if not node_ids:
        return {}
    per_stream_limit = MAX_LOG_BYTES // len(node_ids)
    result = {}
    for node_id, (stdout_name, stderr_name) in zip(node_ids, step_logs):
        stdout = _read_log_tail(ws, stdout_name, per_stream_limit)
        stderr = _read_log_tail(ws, stderr_name, per_stream_limit)
        if stdout is not None or stderr is not None:
            result[node_id] = {"stdout": stdout or "", "stderr": stderr or ""}
    return result


def failing_steps(stderr, node_ids, rule_name_for):
    """Which node names appear in Snakemake-style stderr blocks.

    `rule_name_for` is passed in rather than imported so this module does not
    depend on the emitter; the caller supplies the one transform that exists.

    A tool can forge such a block, so these are diagnostic hints rather than
    proof of a failed node. A rule name that maps to more than one node is
    reported against all of them with a note. Two node ids can collide --
    "a.b" and "a-b" both become "a_b" --
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
        block = "\n\n".join(texts)
        for node_id in owners:
            attributed[node_id] = {
                "rule": rule,
                "stderr": block,
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
