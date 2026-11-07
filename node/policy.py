"""What the Policy Engine says about a study's output rules.

The engine is not part of this repository yet, so a protocol stands where it will
go (section 6.5). The fake is for development and tests only, and says so on the
page: a data manager must not take its verdict for the real one.
"""

from dataclasses import dataclass
from typing import Protocol


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


class PolicyEngine(Protocol):
    name: str

    def check_rules(self, output_rules) -> RulesVerdict:
        ...

    def check(self, run, output_rules) -> list:
        """One FileVerdict for each file of `run.egress_files`."""


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
        allowed = output_rules.get("types", [])
        limit = output_rules.get("max_bytes")
        verdicts = []
        for entry in run.egress_files:
            kind = entry["file"].rpartition(".")[2]
            if kind not in allowed:
                verdicts.append(FileVerdict(entry["file"], "held_back",
                                            "type not allowed by the study"))
            elif limit is not None and entry["size"] > limit:
                verdicts.append(FileVerdict(entry["file"], "held_back",
                                            "larger than the study allows"))
            else:
                verdicts.append(FileVerdict(entry["file"], "pass", "allowed type"))
        return verdicts
