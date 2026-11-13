"""The tables of docs/agent-ui/SPEC.md, section 6.1.

Field names follow ieeta-pt/biochef-coordinator#6 wherever a field comes from a
coordinator document.

Every signed document is kept byte for byte in `raw`. The parsed columns are for
display and queries; the bytes are what signatures cover. Re-serialising parsed
JSON would give different bytes and a signature that no longer verifies, so
`raw` is binary and never rebuilt.

The vocabularies are CHECK constraints rather than a convention in the code,
because two processes write here, and a value only one of them understands is
the kind of thing nobody notices until the page cannot render it.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (JSON, CheckConstraint, Column, Index,
                        LargeBinary, Text, UniqueConstraint, event, inspect,
                        text)
from sqlmodel import Field, Session, SQLModel

from store.errors import ImmutableField
from store.types import UtcDateTime
from store.states import (RUN_STATES, Action, BindingSource, EgressState,
                          MessageKind, RefusalCode, ResultRecipients,
                          SubjectType,
                          VersionState, Verdict)

# Named constraints, because SQLite can only alter a table by rebuilding it and
# Alembic needs the names to do that.
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _one_of(column, values, name=None):
    """A CHECK that `column` holds one of `values`: an Enum, or a set of strings."""
    names = [getattr(value, "value", value) for value in values]
    if not isinstance(values, type):
        names.sort()
    allowed = ", ".join(f"'{value}'" for value in names)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


def _moment(*, nullable):
    extra = {"default": None} if nullable else {}
    return Field(sa_column=Column(UtcDateTime, nullable=nullable), **extra)


def _json(default=None):
    extra = {"default_factory": default} if default else {}
    return Field(sa_column=Column(JSON, nullable=False), **extra)


def _raw():
    return Field(sa_column=Column(LargeBinary, nullable=False))


class Study(SQLModel, table=True):
    __tablename__ = "study"

    id: str = Field(primary_key=True)
    title: Optional[str] = None
    created_at: datetime = _moment(nullable=False)


class StudyVersion(SQLModel, table=True):
    __tablename__ = "study_version"
    __table_args__ = (
        UniqueConstraint("study_id", "version"),
        UniqueConstraint("digest"),
        _one_of("state", VersionState),
        _one_of("result_recipients", ResultRecipients),
        # Only one approved version of a study at a time. Here as well as in the
        # code, because this is the rule the whole key replacement story rests
        # on, and a race between two writers must not be able to break it.
        Index("one_approved_version", "study_id", unique=True,
              sqlite_where=text("state = 'approved'"),
              postgresql_where=text("state = 'approved'")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    study_id: str = Field(foreign_key="study.id")
    version: int
    digest: str
    previous_digest: Optional[str] = None
    raw: bytes = _raw()
    goal: str
    proposer: str
    members: List[Any] = _json()
    nodes: List[Any] = _json()
    datasets: List[str] = _json()
    output_rules: Dict[str, Any] = _json()
    result_recipients: str = ResultRecipients.SUBMITTER.value
    received_at: datetime = _moment(nullable=False)
    state: str = VersionState.PENDING.value
    state_changed_at: datetime = _moment(nullable=False)


class MemberKey(SQLModel, table=True):
    __tablename__ = "member_key"

    study_version_id: int = Field(foreign_key="study_version.id",
                                  primary_key=True)
    member: str = Field(primary_key=True)
    ls_aai_id: str
    signing_key: Dict[str, Any] = _json()
    result_key: str
    binding_token: str = Field(sa_column=Column(Text, nullable=False))
    identity_verified: bool = False
    identity_error: Optional[str] = None
    # Only ever the name claim of a verified binding_token. See
    # repo.mark_identity_verified, the one place that writes it.
    display_name: Optional[str] = None


class WorkflowVersion(SQLModel, table=True):
    __tablename__ = "workflow_version"
    __table_args__ = (
        UniqueConstraint("study_version_id", "digest"),
        _one_of("state", VersionState),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    study_id: str = Field(foreign_key="study.id")
    study_version_id: int = Field(foreign_key="study_version.id")
    digest: str
    raw: bytes = _raw()
    name: Optional[str] = None
    version: Optional[str] = None
    workflowhub_id: Optional[str] = None
    steps: List[Any] = _json()
    received_at: datetime = _moment(nullable=False)
    state: str = VersionState.PENDING.value
    state_changed_at: datetime = _moment(nullable=False)


class Decision(SQLModel, table=True):
    __tablename__ = "decision"
    __table_args__ = (
        _one_of("subject_type", SubjectType),
        _one_of("action", Action),
        Index("decision_subject", "subject_type", "subject_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    subject_type: str
    subject_id: str
    subject_hash: Optional[str] = None
    action: str
    user: str
    comment: Optional[str] = None
    dac_confirmed: Optional[bool] = None
    reason: Optional[str] = None
    at: datetime = _moment(nullable=False)
    object_raw: bytes = _raw()
    signature: str = Field(sa_column=Column(Text, nullable=False))
    # Set in its own transaction just before the POST, so an undo can tell that
    # the decision may already be at the coordinator. Both only ever go from
    # null to a moment (see ONE_WAY).
    posting_started_at: Optional[datetime] = _moment(nullable=True)
    posted_to_coordinator_at: Optional[datetime] = _moment(nullable=True)


class Request(SQLModel, table=True):
    __tablename__ = "request"
    __table_args__ = (
        _one_of("verdict", Verdict),
        _one_of("failed_check", RefusalCode),
        # What repo.set_verdict refuses, refused here too: a refusal and only a
        # refusal names a check.
        CheckConstraint("(COALESCE(verdict, '') = 'refused') = (failed_check IS NOT NULL)",
                        name="refusal_names_a_check"),
        Index("request_received", "received_at"),
    )

    request_id: str = Field(primary_key=True)
    study_id: str
    # As the request names it, not a foreign key: a request can name a version
    # this node never received, and it still has to be recorded and refused.
    study_version: int
    study_digest: str
    workflow_digest: str
    member: str
    node: str
    datasets: List[str] = _json()
    not_after: datetime = _moment(nullable=False)
    payload_digest: str
    raw: bytes = _raw()
    received_at: datetime = _moment(nullable=False)
    verdict: Optional[str] = None
    failed_check: Optional[str] = None
    run_id: Optional[str] = None
    # The same request_id delivered again: it joins the run or repeats the refusal
    # (section 6.3, check 6), and only the count and the last time are kept.
    repeats: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_repeat_at: Optional[datetime] = _moment(nullable=True)


class RunSummary(SQLModel, table=True):
    __tablename__ = "run_summary"
    __table_args__ = (_one_of("egress_state", EgressState),
                      _one_of("state", RUN_STATES))

    run_id: str = Field(primary_key=True)
    request_id: Optional[str] = Field(default=None,
                                      foreign_key="request.request_id")
    state: str
    steps: List[Any] = _json(list)
    started_at: Optional[datetime] = _moment(nullable=True)
    ended_at: Optional[datetime] = _moment(nullable=True)
    exit_code: Optional[int] = None
    workspace_path: Optional[str] = None
    manifest_path: Optional[str] = None
    log_stdout: str = Field(default="", sa_column=Column(Text, nullable=False))
    log_stderr: str = Field(default="", sa_column=Column(Text, nullable=False))
    node_logs: Dict[str, Any] = _json(dict)
    cancel_requested: bool = False
    egress_state: str = EgressState.NOT_READY.value
    egress_files: List[Any] = _json(list)
    airlock_ticket: Optional[str] = None
    airlock_at: Optional[datetime] = _moment(nullable=True)
    retain_until: Optional[datetime] = _moment(nullable=True)


class DatasetBinding(SQLModel, table=True):
    __tablename__ = "dataset_binding"
    __table_args__ = (_one_of("source", BindingSource),)

    dataset_id: str = Field(primary_key=True)
    source: str
    type: str
    location: str
    # The value the data access committee puts in a visa for this dataset, usually
    # a URL. A dataset id is this node's name for the data, and visas do not use it.
    visa_value: Optional[str] = None
    bound_by: str
    bound_at: datetime = _moment(nullable=False)


class OutgoingMessage(SQLModel, table=True):
    """A signed document for the coordinator, kept until something sends it.

    Signed when the decision is made, so what goes out is what was decided then,
    whenever the sending happens.
    """

    __tablename__ = "outgoing_message"
    __table_args__ = (_one_of("kind", MessageKind),
                      UniqueConstraint("run_id", "kind", name="one_message_per_kind"))

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="run_summary.run_id")
    kind: str
    document: bytes = _raw()
    created_at: datetime = _moment(nullable=False)
    sent_at: Optional[datetime] = _moment(nullable=True)


class FederationList(SQLModel, table=True):
    __tablename__ = "federation_list"

    version: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
    raw: bytes = _raw()
    signature: str = Field(sa_column=Column(Text, nullable=False))
    nodes: List[Any] = _json()
    received_at: datetime = _moment(nullable=False)


class NodeStatus(SQLModel, table=True):
    """One row, written by the poll. The page lives in another process."""

    __tablename__ = "node_status"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: int = Field(default=1, primary_key=True)
    last_poll_at: Optional[datetime] = _moment(nullable=True)
    last_success_at: Optional[datetime] = _moment(nullable=True)
    last_error: Optional[str] = None


IMMUTABLE = {
    StudyVersion: ("study_id", "version", "digest", "raw"),
    WorkflowVersion: ("study_id", "study_version_id", "digest", "raw"),
    MemberKey: ("study_version_id", "member", "ls_aai_id", "signing_key",
                "result_key", "binding_token"),
    Request: ("study_id", "study_version", "study_digest", "workflow_digest",
              "payload_digest", "raw"),
    OutgoingMessage: ("run_id", "kind", "document", "created_at"),
    Decision: ("subject_type", "subject_id", "subject_hash", "action", "user",
               "comment", "dac_confirmed", "reason", "at", "object_raw",
               "signature"),
    FederationList: ("version", "raw", "signature", "nodes"),
}
"""Fields no update may touch.

A signed document that changed under its signature, or a decision that now says
something else, is a corrupted record rather than an edit. This guards the
application, not the file: anything that can open the database can write to it.
"""


ONE_WAY = {
    Decision: ("posting_started_at", "posted_to_coordinator_at"),
}
"""Fields that may be set once, from null, and never changed or cleared.

Setting `posted_to_coordinator_at` back to null would make a decision the
coordinator already holds look undoable.
"""

EGRESS_FIELDS = ("egress_files", "airlock_ticket", "airlock_at")
"""What a run's review is about. Written while the rules run, then fixed:
release reads the passing files back from `egress_files`, so a later write
would change what is sent."""

_EGRESS_OPEN = {EgressState.NOT_READY.value, EgressState.RULES_PENDING.value}


def _before(history):
    """The value an attribute had when it was loaded, if it was."""
    old = list(history.deleted) or list(history.unchanged)
    return old[0] if old else None


@event.listens_for(Session, "before_flush")
def _refuse_changes_to_immutable_fields(session, _context, _instances):
    for instance in session.dirty:
        kind = type(instance)
        state = inspect(instance)
        for name in IMMUTABLE.get(kind, ()):
            if state.attrs[name].history.has_changes():
                raise ImmutableField(f"{kind.__name__}.{name}")
        for name in ONE_WAY.get(kind, ()):
            history = state.attrs[name].history
            if history.has_changes() and _before(history) is not None:
                raise ImmutableField(f"{kind.__name__}.{name} is already set")
        if kind is RunSummary:
            egress = state.attrs["egress_state"].history
            stages = {_before(egress), instance.egress_state}
            if not stages <= _EGRESS_OPEN and any(
                    state.attrs[name].history.has_changes()
                    for name in EGRESS_FIELDS):
                raise ImmutableField(
                    f"RunSummary {instance.run_id}: egress files are fixed once "
                    f"the rules have run")


__all__ = ["Decision", "DatasetBinding", "FederationList", "MemberKey",
           "NodeStatus", "Request", "RunSummary", "Study", "StudyVersion",
           "UtcDateTime", "WorkflowVersion"]
