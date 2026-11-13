"""Connections to the node's database.

SQLite by default, opened by two processes at once, which sets three things that
its defaults get wrong for this use:

WAL, so the page can read while the API process writes.

BEGIN IMMEDIATE for anything that writes. SQLite's default takes the write lock
at the first write, and a transaction that read something first then fails at
once with "database is locked" if another writer got there, without waiting for
busy_timeout. An approval reads before it writes, so it would lose that race.

foreign_keys, which SQLite leaves off on every new connection.
"""

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session

from store.errors import StoreError

DEFAULT_URL = "sqlite:////var/lib/biochef/agent.db"

BUSY_TIMEOUT_MS = 5000
"""How long a writer waits for another. Transactions here are short."""

_engines = {}


def url():
    return (os.getenv("BIOCHEF_DB_URL") or "").strip() or DEFAULT_URL


def _configure_sqlite(engine):
    @event.listens_for(engine, "connect")
    def _on_connect(connection, _record):
        # Hands transaction control to the "begin" hook below. Left alone, the
        # driver decides when to BEGIN and the hook's statement would be a
        # second one.
        connection.isolation_level = None
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        writing = connection.get_execution_options().get("write", False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if writing else "BEGIN")


def make_engine(database_url=None):
    """An engine for this URL. Refuses anything but SQLite, and a SQLite file
    that could not be created.

    Only SQLite is supported: the write lock above is how concurrent approvals
    are kept apart, and on another database they would end in a raw integrity
    error from the partial index, with no row locks taken. SQLite's own message
    for a missing directory is "unable to open database file", which does not
    say which file or why.
    """
    parsed = make_url(database_url or url())
    if parsed.get_backend_name() != "sqlite":
        raise StoreError(f"BIOCHEF_DB_URL must be a sqlite:// URL, not "
                         f"{parsed.get_backend_name()}: only SQLite is supported")

    if parsed.database and parsed.database != ":memory:":
        parent = Path(parsed.database).parent
        if not parent.is_dir():
            raise StoreError(
                f"BIOCHEF_DB_URL points into {parent}, which does not exist"
            )
    engine = create_engine(parsed)
    _configure_sqlite(engine)
    return engine


def engine() -> Engine:
    """The engine for the configured URL, made on first use."""
    current = url()
    if current not in _engines:
        _engines[current] = make_engine(current)
    return _engines[current]


@contextmanager
def _session(bind):
    # expire_on_commit off, so what a function returns can be read after its
    # transaction ended. Nothing here relies on seeing later writes through it.
    with Session(bind, expire_on_commit=False) as session:
        with session.begin():
            yield session


def reading(bind=None):
    """A transaction that only reads."""
    return _session(bind or engine())


def writing(bind=None):
    """A transaction that writes: commits at the end, rolls back on an exception."""
    return _session((bind or engine()).execution_options(write=True))
