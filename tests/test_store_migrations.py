"""The schema moves up and down, and matches the models (agent UI, part 1)."""

import os
import subprocess
import sys

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlmodel import SQLModel

from tests.store_support import REPO_ROOT, fresh_database

from store import db, migrate

TABLES = {"study", "study_version", "member_key", "workflow_version", "decision",
          "request", "run_summary", "dataset_binding", "federation_list",
          "node_status", "outgoing_message"}


def test_upgrade_creates_every_table(tmp_path):
    _, engine = fresh_database(tmp_path)
    assert TABLES <= set(inspect(engine).get_table_names())
    assert migrate.is_current(engine)


def test_downgrade_removes_them_and_upgrade_returns(tmp_path):
    url, engine = fresh_database(tmp_path)
    migrate.downgrade(url)
    assert not TABLES & set(inspect(engine).get_table_names())
    assert not migrate.is_current(engine)
    migrate.upgrade(url)
    assert TABLES <= set(inspect(engine).get_table_names())


def test_the_migrations_match_the_models(tmp_path):
    """A column added to a model and to no migration fails only in production."""
    _, engine = fresh_database(tmp_path)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True})
        assert compare_metadata(context, SQLModel.metadata) == []


def test_the_partial_index_survives_migration(tmp_path):
    """compare_metadata does not look at index conditions, so this does."""
    _, engine = fresh_database(tmp_path)
    with engine.connect() as connection:
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'one_approved_version'"
        ).scalar()
    assert "UNIQUE" in sql and "state = 'approved'" in sql


def test_the_command_line_migrates_the_configured_database(tmp_path):
    url = f"sqlite:///{tmp_path}/cli.db"
    subprocess.run([sys.executable, "-m", "store", "migrate"], cwd=REPO_ROOT,
                   env={**os.environ, "BIOCHEF_DB_URL": url}, check=True)
    engine = db.make_engine(url)
    assert migrate.is_current(engine)


def test_joined_rows_go_and_the_rest_keep_their_verdict(tmp_path):
    """0003: a joined row was a second request_id, which is now a request of its own."""
    url = f"sqlite:///{tmp_path}/old.db"
    migrate.upgrade(url, "0002")
    engine = db.make_engine(url)
    columns = ("request_id, study_id, study_version, study_digest, workflow_digest, "
               "member, node, datasets, not_after, payload_digest, raw, received_at, "
               "verdict, joined_request_id")
    values = ("'{0}', 's', 1, 'd', 'w', 'm', 'n', '[]', '2026-01-01 00:00:00', 'p', "
              "x'00', '2026-01-01 00:00:00', '{1}', {2}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"INSERT INTO request ({columns}) VALUES ({values.format('req_1', 'accepted', 'NULL')})")
        connection.exec_driver_sql(
            f"INSERT INTO request ({columns}) VALUES ({values.format('req_2', 'joined', chr(39) + 'req_1' + chr(39))})")
    migrate.upgrade(url)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT request_id, verdict, repeats FROM request").all()
        names = {column["name"] for column in inspect(connection).get_columns("request")}
    assert rows == [("req_1", "accepted", 0)]
    assert "joined_request_id" not in names
    with pytest.raises(Exception, match="CHECK"):
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE request SET verdict = 'joined'")


def test_a_password_with_a_percent_sign_reaches_alembic():
    config = migrate._config("postgresql://u:p%40ss@host/db")
    assert config.get_main_option("sqlalchemy.url") == "postgresql://u:p%40ss@host/db"


def test_a_missing_directory_is_named():
    with pytest.raises(Exception, match="does not exist"):
        db.make_engine("sqlite:////no/such/place/agent.db")


def test_importing_the_store_opens_nothing(tmp_path):
    """The API must not start needing a database because a module was imported."""
    target = tmp_path / "untouched.db"
    code = "import store.repo, store.seed, store.migrate"
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True,
                   env={**os.environ, "BIOCHEF_DB_URL": f"sqlite:///{target}"})
    assert not target.exists()
