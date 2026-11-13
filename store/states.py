"""The vocabularies of the store, and which states may follow which.

Written out for the same reason as runs.ALLOWED: the interesting bugs in a state
machine are the transitions nobody thought about, such as a refused version
turning up approved. Absent from these tables means it may not happen.
"""

from enum import Enum


class VersionState(str, Enum):
    """Where a study version or a workflow version stands on this node."""

    PENDING = "pending"
    APPROVED = "approved"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


STUDY_ALLOWED = {
    # A version still waiting when a newer one is approved goes with the one it
    # would have replaced: approving it later is refused, so it would wait for
    # ever.
    VersionState.PENDING: {VersionState.APPROVED, VersionState.REFUSED,
                           VersionState.SUPERSEDED},
    VersionState.APPROVED: {VersionState.WITHDRAWN, VersionState.SUPERSEDED},
    VersionState.REFUSED: set(),
    VersionState.WITHDRAWN: set(),
    VersionState.SUPERSEDED: set(),
}

WORKFLOW_ALLOWED = {
    # A workflow still waiting when its study version is replaced or withdrawn
    # goes with it: approvals do not carry over, so nothing would ever decide it.
    VersionState.PENDING: {VersionState.APPROVED, VersionState.REFUSED,
                           VersionState.SUPERSEDED, VersionState.WITHDRAWN},
    VersionState.APPROVED: {VersionState.WITHDRAWN, VersionState.SUPERSEDED},
    VersionState.REFUSED: set(),
    VersionState.WITHDRAWN: set(),
    VersionState.SUPERSEDED: set(),
}


class ResultRecipients(str, Enum):
    """Who a result is encrypted for. Only the member who submitted the request,
    for now; `all_members` waits for ieeta-pt/biochef-coordinator#6."""

    SUBMITTER = "submitter"


class SubjectType(str, Enum):
    STUDY_VERSION = "study_version"
    WORKFLOW_VERSION = "workflow_version"
    REQUEST = "request"
    RUN = "run"
    EGRESS = "egress"
    DATASET_BINDING = "dataset_binding"


class Action(str, Enum):
    APPROVE = "approve"
    REFUSE = "refuse"
    WITHDRAW = "withdraw"
    RELEASE = "release"
    WITHHOLD = "withhold"
    BIND = "bind"
    UNBIND = "unbind"
    CANCEL = "cancel"


class Verdict(str, Enum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    JOINED = "joined"


class RefusalCode(str, Enum):
    """The request checks of section 6.3, in the order they run."""

    BAD_SIGNATURE = "bad_signature"
    WRONG_NODE = "wrong_node"
    STUDY_NOT_APPROVED = "study_not_approved"
    WORKFLOW_NOT_APPROVED = "workflow_not_approved"
    EXPIRED = "expired"
    REPLAY = "replay"
    CANNOT_DECRYPT = "cannot_decrypt"
    HASH_MISMATCH = "hash_mismatch"
    VISA_MISSING = "visa_missing"
    DATASET_NOT_BOUND = "dataset_not_bound"


class EgressState(str, Enum):
    NOT_READY = "not_ready"
    RULES_PENDING = "rules_pending"
    AWAITING_AIRLOCK = "awaiting_airlock"
    AWAITING_REVIEW = "awaiting_review"
    RELEASED = "released"
    WITHHELD = "withheld"
    SENT = "sent"


EGRESS_ALLOWED = {
    EgressState.NOT_READY: {EgressState.RULES_PENDING},
    EgressState.RULES_PENDING: {EgressState.AWAITING_AIRLOCK,
                                EgressState.AWAITING_REVIEW},
    EgressState.AWAITING_AIRLOCK: {EgressState.RELEASED, EgressState.WITHHELD},
    EgressState.AWAITING_REVIEW: {EgressState.RELEASED, EgressState.WITHHELD},
    EgressState.RELEASED: {EgressState.SENT},
    EgressState.WITHHELD: set(),
    EgressState.SENT: set(),
}

EGRESS_FINISHED = frozenset({EgressState.SENT, EgressState.WITHHELD})
"""Where the retention clock starts. Before this, outputs are still needed."""

RUN_STATES = frozenset({"QUEUED", "INITIALIZING", "RUNNING", "COMPLETE",
                        "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELING",
                        "CANCELED"})
"""The WES states of section 6.4, as named by runs.RunState."""

CANCELLABLE_RUN_STATES = frozenset({"QUEUED", "INITIALIZING", "RUNNING"})
"""As named by runs.RunState. Strings, so the store need not import the runner."""

BUSY_RUN_STATES = frozenset({"INITIALIZING", "RUNNING", "CANCELING"})
"""A run holding a slot of BIOCHEF_MAX_CONCURRENT_RUNS."""


class BindingSource(str, Enum):
    CONFIG = "config"
    PAGE = "page"
