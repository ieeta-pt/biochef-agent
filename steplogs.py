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
