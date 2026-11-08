"""The TES API's tasks in the node's database, so a restart does not forget them.

The one place the listener opens the database, and it touches one table, `tes_task`:
no study, request, decision, dataset binding or run of the federated side is read or
written from here. A test holds it to that.

What is kept is what the caller was told: the plan's name, image, command and the
URLs of its inputs and outputs, the state, the times, the logs and the sizes. The
inline content of an input is not kept, and no setting or secret of the node's was
ever in a task.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, func
from sqlmodel import select

from store import db, migrate
from store.models import TesTask


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
