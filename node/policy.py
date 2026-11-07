"""What the output rules of a study say about a run's files (section 6.5).

The Policy Engine is not part of this repository yet, so a protocol stands where
it will go. Until it is connected the agent checks what it can itself: `types`
and `max_bytes` need only a file's name and size. `min_count` and
`aggregates_only` need the contents read, which is the engine's job, so those
are marked not verified on every file they apply to. A person releasing such a
run confirms that explicitly, and an airlock receives the same marking.

The fake is for development and tests only, and says so on the page: a data
manager must not take its verdict for the real one.
"""

from dataclasses import dataclass, field
from typing import Protocol

UNVERIFIED_HERE = ("min_count", "aggregates_only")
"""The rules only the Policy Engine can check, in the order the page lists them."""


@dataclass(frozen=True)
class RulesVerdict:
    acceptable: bool
    text: str


@dataclass(frozen=True)
class FileVerdict:
    file: str
    verdict: str
    """`pass` or `held_back`."""
    reason: str
    unverified: tuple = field(default=())
    """Rules of the study nobody checked for this file."""


class PolicyEngine(Protocol):
    name: str

    def check_rules(self, output_rules) -> RulesVerdict:
        ...

    def check(self, run, output_rules) -> list:
        """One FileVerdict for each file of `run.egress_files`."""


def _type_and_size(entry, output_rules, unverified=()):
    """What a name and a size can say: the type is the file's last suffix."""
    allowed = output_rules.get("types", [])
    limit = output_rules.get("max_bytes")
    kind = entry["file"].rpartition(".")[2]
    if kind not in allowed:
        return FileVerdict(entry["file"], "held_back", "type not allowed by the study")
    if limit is not None and entry["size"] > limit:
        return FileVerdict(entry["file"], "held_back", "larger than the study allows")
    return FileVerdict(entry["file"], "pass", "allowed type", tuple(unverified))


def unverified_rules(output_rules):
    """The rules a study sets that only the Policy Engine can check."""
    found = []
    if output_rules.get("min_count") is not None:
        found.append("min_count")
    if output_rules.get("aggregates_only"):
        found.append("aggregates_only")
    return tuple(found)


class LocalRules:
    """The agent's own check, while no Policy Engine is connected."""

    name = "The agent's own check"

    def check_rules(self, output_rules):
        open_rules = unverified_rules(output_rules)
        if not open_rules:
            return RulesVerdict(True, "types and sizes are checked here")
        return RulesVerdict(False, "types and sizes are checked here; "
                            f"{' and '.join(open_rules)} cannot be until the "
                            "Policy Engine is connected")

    def check(self, run, output_rules):
        open_rules = unverified_rules(output_rules)
        return [_type_and_size(entry, output_rules, open_rules)
                for entry in run.egress_files]


class FakePolicyEngine:
    """A local minimum for `min_count`, which is enough to show both verdicts."""

    name = "Fake Policy Engine"

    def __init__(self, min_count=10):
        self.min_count = min_count

    def check_rules(self, output_rules):
        if output_rules.get("min_count", 0) < self.min_count:
            return RulesVerdict(False, "weaker than this node's local rules")
        return RulesVerdict(True, "meets this node's local rules")

    def check(self, run, output_rules):
        return [_type_and_size(entry, output_rules) for entry in run.egress_files]
