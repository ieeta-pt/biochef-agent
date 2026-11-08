"""The TES API's tasks in the node's database, so a restart does not forget them.

The one place the listener opens the database, and it touches two tables: `tes_task`,
its tasks, and `tes_listener`, the lease that lets one listener hold the database.
No study, request, decision, dataset binding or run of the federated side is read or
written from here. A test holds it to that.

What is kept is what the caller was told: the plan's name, image, command and the
URLs of its inputs and outputs, the state, the times, the logs and the sizes. The
inline content of an input is not kept, and no setting or secret of the node's was
ever in a task.
"""

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from store import db, migrate
from store.models import TesListener, TesTask

LEASE_S = 30
"""How long a listener's place stays its own without a heartbeat. A listener killed
without releasing it is replaced after this long."""

BEAT_S = 10
"""How often a running listener renews it: three chances to miss one."""


class RecordError(Exception):
    """The database would not take or give a task."""


def _moment(text: Optional[str]) -> Optional[datetime]:
    return None if not text else datetime.fromisoformat(text)


def _text(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat()


def check(bind) -> None:
    """Refuse to go on against a schema older than this code, as the page does."""
    try:
        current = migrate.is_current(bind)
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the database cannot be read: {type(error).__name__}") from None
    if not current:
        raise RecordError("the database schema is not current; run: "
                          "python -m store migrate")


def row_of(task) -> TesTask:
    plan = task.plan
    return TesTask(
        id=task.id, owner=task.owner, name=plan.name, description=plan.description,
        tags=dict(plan.tags), image=plan.image, command=list(plan.command),
        inputs=[{"kind": i.kind, "relative": i.relative, "url": i.url}
                for i in plan.inputs if i.url],
        outputs=[{"kind": o.kind, "relative": o.relative, "url": o.url}
                 for o in plan.outputs],
        state=task.state, created_at=_moment(task.created),
        started_at=_moment(task.started), ended_at=_moment(task.ended),
        exit_code=task.exit_code, stdout=task.stdout, stderr=task.stderr,
        system_logs=list(task.system_logs), sizes=list(task.sizes))


def save(bind, task) -> None:
    """The task as it is now, written whole. Raises RecordError."""
    try:
        with db.writing(bind) as session:
            session.merge(row_of(task))
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the task could not be kept: {type(error).__name__}") \
            from None


def prune(bind, keep: int) -> None:
    """Forget the oldest finished tasks beyond `keep`. Unfinished ones are never dropped."""
    try:
        with db.writing(bind) as session:
            total = session.exec(select(func.count()).select_from(TesTask)).one()
            if total <= keep:
                return
            oldest = session.exec(
                select(TesTask.id).where(TesTask.state.in_(
                    ("COMPLETE", "EXECUTOR_ERROR", "SYSTEM_ERROR", "CANCELED")))
                .order_by(TesTask.created_at).limit(total - keep)).all()
            if oldest:
                session.execute(delete(TesTask).where(TesTask.id.in_(oldest)))
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"old tasks could not be forgotten: "
                          f"{type(error).__name__}") from None


def load(bind, keep: int) -> List[TesTask]:
    """The newest `keep` tasks, oldest first."""
    try:
        with db.reading(bind) as session:
            rows = session.exec(select(TesTask).order_by(TesTask.created_at.desc())
                                .limit(keep)).all()
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the tasks could not be read: {type(error).__name__}") \
            from None
    return list(reversed(rows))


# --------------------------------------------------------------------------
# one listener per database


class Held(RecordError):
    """Another listener holds the database."""


class Lease(NamedTuple):
    holder: str
    host: str
    pid: int
    started_at: datetime
    heartbeat_at: datetime

    def expires(self) -> datetime:
        return self.heartbeat_at + timedelta(seconds=LEASE_S)


def _utc(now):
    return datetime.now(timezone.utc) if now is None else now


def _refusal(lease: Lease) -> Held:
    return Held(
        f"another TES listener holds this database: pid {lease.pid} on "
        f"{lease.host}, since {lease.started_at.isoformat(timespec='seconds')}, "
        f"last heard from at {lease.heartbeat_at.isoformat(timespec='seconds')}. "
        f"Starting a second one would end the first one's running tasks. Stop the "
        f"other, or if it is gone wait until {lease.expires().isoformat(timespec='seconds')}, "
        f"when its place expires")


def _lease(row) -> Lease:
    return Lease(row.holder, row.host, row.pid, row.started_at, row.heartbeat_at)


def current(bind, now=None) -> Optional[Lease]:
    """The lease some listener holds and has not let expire, or None."""
    try:
        with db.reading(bind) as session:
            row = session.get(TesListener, 1)
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the lease could not be read: {type(error).__name__}") from None
    if row is None or _lease(row).expires() <= _utc(now):
        return None
    return _lease(row)


def preflight(bind, now=None) -> None:
    """Refuse, with the message, before anything starts; acquire() is what binds."""
    lease = current(bind, now)
    if lease is not None:
        raise _refusal(lease)


def acquire(bind, holder: str, now=None) -> Optional[Lease]:
    """Take the database's one place, or raise Held. Returns the expired lease it
    replaced, if there was one, so that the takeover can be put on the record.

    The write is conditional on the row being as it was read, so two listeners that
    start together cannot both win, on SQLite or on a server database.
    """
    moment = _utc(now)
    try:
        with db.writing(bind) as session:
            row = session.get(TesListener, 1)
            if row is None:
                session.add(TesListener(id=1, holder=holder, host=socket.gethostname(),
                                        pid=os.getpid(), started_at=moment,
                                        heartbeat_at=moment))
                session.flush()
                return None
            lease = _lease(row)
            previous_holder, previous_beat = row.holder, row.heartbeat_at
            if previous_holder != holder and lease.expires() > moment:
                raise _refusal(lease)
            taken = session.execute(
                update(TesListener)
                .where(TesListener.id == 1, TesListener.holder == previous_holder,
                       TesListener.heartbeat_at == previous_beat)
                .values(holder=holder, host=socket.gethostname(), pid=os.getpid(),
                        started_at=moment, heartbeat_at=moment))
            if taken.rowcount != 1:
                raise Held("another TES listener took the database's place at the "
                           "same moment")
            return lease if previous_holder != holder else None
    except RecordError:
        raise
    except IntegrityError:
        # A second row at once: someone else got there first.
        raise Held("another TES listener took the database's place at the same "
                   "moment") from None
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the database's place could not be taken: "
                          f"{type(error).__name__}") from None


def beat(bind, holder: str, now=None) -> bool:
    """Renew the place. False if it is no longer this listener's."""
    try:
        with db.writing(bind) as session:
            done = session.execute(
                update(TesListener).where(TesListener.id == 1,
                                          TesListener.holder == holder)
                .values(heartbeat_at=_utc(now)))
            return done.rowcount == 1
    except Exception as error:                      # noqa: BLE001
        raise RecordError(f"the lease could not be renewed: {type(error).__name__}") \
            from None


def release(bind, holder: str) -> None:
    """Give the place up, if it is still this listener's. Never raises."""
    try:
        with db.writing(bind) as session:
            session.execute(delete(TesListener).where(TesListener.id == 1,
                                                      TesListener.holder == holder))
    except Exception:                               # noqa: BLE001
        pass
