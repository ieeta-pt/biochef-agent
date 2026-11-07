"""How Alembic reaches the database. Online only: a node migrates its own file."""

from alembic import context
from sqlmodel import SQLModel

import store.models  # noqa: F401  (registers the tables on the metadata)
from store import db

if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")

engine = db.make_engine(context.config.get_main_option("sqlalchemy.url"))

with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        # SQLite can only alter a table by rebuilding it.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()

engine.dispose()
