"""Moving the database between schema versions.

Alembic from the first table, and never `create_all`: a node's database holds
signed approvals, so it has to be upgraded in place rather than recreated.

Configured here rather than in an alembic.ini, so that the same code path serves
the command line, the tests and whatever starts the service, and so the URL
comes only from BIOCHEF_DB_URL.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from store import db

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _config(database_url=None):
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("path_separator", "os")
    # Config goes through ConfigParser, where % starts an interpolation, and a
    # password in a URL may well contain one.
    config.set_main_option("sqlalchemy.url",
                           (database_url or db.url()).replace("%", "%%"))
    return config


def upgrade(database_url=None, revision="head"):
    command.upgrade(_config(database_url), revision)


def downgrade(database_url=None, revision="base"):
    command.downgrade(_config(database_url), revision)


def head():
    return ScriptDirectory.from_config(_config("sqlite://")).get_current_head()


def current(bind=None):
    with (bind or db.engine()).connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_current(bind=None):
    """Whether the schema is the one this code expects.

    For whatever starts the service: running against an older schema fails later
    and somewhere less obvious.
    """
    return current(bind) == head()
