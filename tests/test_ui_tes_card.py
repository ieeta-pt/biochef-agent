"""The TES API on the Node screen: read only, from the database (SPEC.md, 14.4)."""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import audit
from store import db, repo
from store.fixtures import mock
from store.models import TesListener, TesTask
from store.states import TES_LEASE_SECONDS
from tests.store_support import fresh_database
from tests.ui_support import client_for, dev_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = "registry.example/tools@sha256:" + "ab12cd34" * 8


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    return fresh_database(tmp_path)[1]


def card(engine):
    page = client_for(dev_settings(node_name=mock.NODE), bind=engine).get("/ui/node").text
    start = page.index('aria-label="TES API"')
    return page[start:page.index('aria-label="Audit check"')]


def lease(engine, *, seconds_ago=1, pid=4242, host="tre-agent-1", started_hours_ago=2):
    now = datetime.now(timezone.utc)
    with db.writing(engine) as session:
        session.add(TesListener(id=1, holder="h", host=host, pid=pid,
                                started_at=now - timedelta(hours=started_hours_ago),
                                heartbeat_at=now - timedelta(seconds=seconds_ago)))


def task(engine, number, state="COMPLETE", owner="alice", image=IMAGE, name="",
         command=("true",), stdout=""):
    with db.writing(engine) as session:
        session.add(TesTask(
            id=f"{number:08x}" + "0" * 24, owner=owner, name=name, image=image,
            command=list(command), state=state, stdout=stdout,
            created_at=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
            + timedelta(minutes=number)))


def test_with_no_listener_and_no_task_it_says_so(engine):
    text = card(engine)
    assert "Not running" in text and "No TES API has held this database." in text
    assert "No task has been sent to this node." in text
    assert "Tasks by state" not in text


def test_a_listener_that_renews_is_running_and_says_who_and_since_when(engine):
    lease(engine, seconds_ago=3, pid=4242, host="tre-agent-1")
    text = card(engine)
    assert ">Running<" in text and "text-ok" in text
    assert "pid 4242 on tre-agent-1" in text and "Since " in text
    assert "Last heard from 3 s ago" in text


def test_a_listener_whose_place_has_expired_is_not_running(engine):
    lease(engine, seconds_ago=TES_LEASE_SECONDS + 5, pid=77, host="old-host")
    text = card(engine)
    assert ">Not running<" in text and "text-bad" in text
    assert "Last held by pid 77 on old-host" in text
    assert ">Running<" not in text


def test_the_place_expires_at_the_lease_the_listener_uses(engine):
    from tesapi import records
    assert TES_LEASE_SECONDS == records.LEASE_S, "one number, read by both"
    lease(engine, seconds_ago=TES_LEASE_SECONDS - 2)
    assert ">Running<" in card(engine)
    # Rewritten, not added: the lease is one row.
    with db.writing(engine) as session:
        row = session.get(TesListener, 1)
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=TES_LEASE_SECONDS + 1)
        session.add(row)
    assert ">Not running<" in card(engine)


def test_the_counts_by_state_are_listed_in_the_order_of_a_task_s_life(engine):
    for number, state in enumerate(["COMPLETE", "COMPLETE", "SYSTEM_ERROR", "RUNNING",
                                    "QUEUED", "CANCELED", "EXECUTOR_ERROR"]):
        task(engine, number, state)
    text = card(engine)
    order = ["QUEUED 1", "RUNNING 1", "COMPLETE 2", "EXECUTOR_ERROR 1",
             "SYSTEM_ERROR 1", "CANCELED 1"]
    positions = [text.index(f">{item}<") for item in order]
    assert positions == sorted(positions)
    assert ">INITIALIZING" not in text and ">CANCELING" not in text, "no empty counts"


def test_the_states_have_their_colours(engine):
    for number, state in enumerate(["COMPLETE", "SYSTEM_ERROR", "RUNNING", "CANCELED"]):
        task(engine, number, state)
    text = card(engine)
    for state, kind in (("COMPLETE", "ok"), ("SYSTEM_ERROR", "bad"),
                        ("RUNNING", "info"), ("CANCELED", "mute")):
        assert f'<span class="pill pill-{kind}">{state}</span>' in text


def test_the_newest_ten_are_shown_with_owner_image_and_state(engine):
    for number in range(13):
        task(engine, number, owner="alice" if number % 2 else "bob", name=f"job{number}")
    text = card(engine)
    assert text.count("<tr><td") == 10
    assert "job12" in text and "job3<" in text and "job2<" not in text
    assert text.index("job12") < text.index("job11"), "newest first"
    assert "The newest 10 of 13." in text
    assert "alice" in text and "bob" in text
    assert "registry.example/tools@sha256:ab12…cd34" in text, "the digest is shortened"
    assert "ab12cd34" * 8 not in text


def test_no_note_about_the_newest_when_all_are_shown(engine):
    task(engine, 1)
    assert "The newest" not in card(engine)


def test_a_command_or_a_log_never_reaches_the_page(engine):
    task(engine, 1, command=("sh", "-c", "echo a-secret-argument"),
         stdout="a-secret-log-line")
    page = client_for(dev_settings(node_name=mock.NODE), bind=engine).get("/ui/node").text
    assert "a-secret-argument" not in page and "a-secret-log-line" not in page


def test_the_query_reads_no_command_and_no_log(engine):
    task(engine, 1, command=("sh", "-c", "x"), stdout="log")
    with db.reading(engine) as session:
        rows = repo.latest_tes_tasks(session)
    assert set(rows[0]._fields) == {"id", "name", "owner", "image", "state", "created_at"}


def test_the_card_changes_nothing_and_offers_nothing(engine):
    lease(engine)
    task(engine, 1)
    text = card(engine)
    assert "<form" not in text and "<button" not in text and "hx-post" not in text
    with db.reading(engine) as session:
        assert repo.tes_task_counts(session) == {"COMPLETE": 1}
        assert repo.tes_listener(session).holder == "h"
    web = client_for(dev_settings(node_name=mock.NODE), bind=engine)
    for route in ("/ui/_/tes", "/ui/tes", "/ui/node/tes"):
        assert web.post(route).status_code in (403, 404, 405)


def test_the_ui_does_not_import_the_listener():
    code = ("import sys, ui.app, ui.views; "
            "bad = [m for m in ('tesapi', 'tesapi.app', 'tesapi.tasks') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


# --------------------------------------------------------------------------
# the events of the listener are in words in the audit log


@pytest.mark.parametrize("fields,words", [
    (dict(event="tes.start"), "TES API started"),
    (dict(event="tes.task.created"), "TES task created"),
    (dict(event="tes.task.state", state="COMPLETE"), "TES task complete"),
    (dict(event="tes.task.state", state="SYSTEM_ERROR"), "TES task system_error"),
    (dict(event="tes.task.refused"), "TES task refused"),
    (dict(event="tes.auth.refused"), "TES API call refused, no valid token"),
    (dict(event="tes.lock.acquired", took_over_from=None), "TES API took the database<"),
    (dict(event="tes.lock.acquired", took_over_from={"pid": 1}),
     "TES API took the database over from a stopped listener"),
    (dict(event="tes.lock.lost", reason="x"), "TES API lost the database"),
])
def test_the_listeners_events_are_in_words(engine, fields, words):
    fields = dict(fields)
    audit.record(fields.pop("event"), source="tes", task_id="t-1234", caller="alice",
                 **fields)
    page = client_for(dev_settings(node_name=mock.NODE), bind=engine).get("/ui/node").text
    log = page[page.index("Append only"):]
    assert words in log
    assert "<td>tes." not in log
    assert '<td class="mono">t-1234</td>' in log and "<td>alice</td>" in log
