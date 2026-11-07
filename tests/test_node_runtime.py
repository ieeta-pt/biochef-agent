"""The API process's side of the database channel (section 3.1 and part 7)."""

import json
from datetime import timedelta

import pytest

import main
from node import egress, runtime
from node.keys import generate
from node.policy import FakePolicyEngine
from retention import Retained
from runs import RunState, RunStore
from store import db, repo
from store.states import EgressState, Verdict
from tests.store_support import NOW, add_version, fresh_database
from workspace import Workspace

KEYS = generate()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    return fresh_database(tmp_path)[1]


@pytest.fixture
def store(engine):
    runs = RunStore()
    runs.observer = runtime.RunMirror(engine)
    return runs


def row_of(engine, run_id):
    with db.reading(engine) as session:
        return repo.get_run(session, run_id)


def test_a_new_run_appears_in_the_store(engine, store):
    run = store.create()
    assert row_of(engine, run.run_id).state == "QUEUED"


def test_each_state_is_mirrored(engine, store):
    run = store.create()
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.RUNNING)
    assert row_of(engine, run.run_id).state == "RUNNING"
    assert row_of(engine, run.run_id).started_at is not None
    store.advance(run.run_id, RunState.COMPLETE)
    row = row_of(engine, run.run_id)
    assert row.state == "COMPLETE" and row.ended_at is not None


def test_logs_are_written_capped_by_the_store(engine, store):
    run = store.create()
    store.record_logs(run.run_id, "x" * 3_000_000, "err", {}, {"qc": {"stdout": "a"}},
                      exit_code=3)
    row = row_of(engine, run.run_id)
    assert len(row.log_stdout) <= main.clamp("x" * 3_000_000).__len__()
    assert row.log_stderr == "err" and row.exit_code == 3
    assert row.node_logs == {"qc": {"stdout": "a"}}


def test_step_times_are_stamped_as_they_change(engine, store):
    run = store.create()
    store.record_progress(run.run_id, {"qc": "RUNNING"})
    first = row_of(engine, run.run_id).steps[0]
    assert first["started_at"] is not None and first["ended_at"] is None
    store.record_progress(run.run_id, {"qc": "COMPLETE"})
    last = row_of(engine, run.run_id).steps[0]
    assert last["state"] == "COMPLETE" and last["ended_at"] is not None
    assert last["started_at"] == first["started_at"]


def test_a_broken_database_does_not_stop_a_run(tmp_path):
    runs = RunStore()

    class Down:
        def execution_options(self, **options):
            raise OSError("no database")

    runs.observer = runtime.RunMirror(Down())
    run = runs.create()
    assert runs.advance(run.run_id, RunState.INITIALIZING).state is RunState.INITIALIZING


def test_an_agent_with_no_mirror_behaves_as_before():
    runs = RunStore()
    assert runs.observer is None
    assert runs.create().state is RunState.QUEUED


def test_a_kept_workspace_is_recorded_with_its_manifest(engine, store, tmp_path):
    run = store.create()
    ws = Workspace(str(tmp_path))
    ws.write_bytes("tool-out", b"12345")
    store.observer.workspace_kept(run.run_id, ws, {"runner": "subprocess"},
                                  {"tool-1": {"out": "tool-out"}})
    row = row_of(engine, run.run_id)
    assert row.workspace_path == ws.path
    assert json.load(open(row.manifest_path)) == {"runner": "subprocess"}
    assert row.egress_files == [{"file": "tool-out", "size": 5, "verdict": None,
                                 "reason": None}]


# Retention


def workspace(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return Workspace(str(folder))


def test_a_held_workspace_is_never_evicted_by_time(tmp_path):
    kept = Retained(keep_seconds=10, max_retained=5)
    ws = workspace(tmp_path, "a")
    kept.keep("a", ws, now=0, hold=True)
    assert kept.workspace("a", now=10_000) is ws


def test_a_held_workspace_is_never_evicted_by_count(tmp_path):
    kept = Retained(keep_seconds=1000, max_retained=1)
    ws = workspace(tmp_path, "a")
    kept.keep("a", ws, now=0, hold=True)
    kept.keep("b", workspace(tmp_path, "b"), now=1)
    assert kept.workspace("a", now=2) is ws
    assert kept.workspace("b", now=2) is None


def test_the_clock_starts_when_egress_is_over(tmp_path):
    kept = Retained(keep_seconds=10, max_retained=5)
    ws = workspace(tmp_path, "a")
    kept.keep("a", ws, now=0, hold=True)
    kept.start_clock("a", 100)
    assert kept.workspace("a", now=105) is ws
    assert kept.workspace("a", now=110) is None


def test_starting_the_clock_twice_keeps_the_first(tmp_path):
    kept = Retained(keep_seconds=10, max_retained=5)
    kept.keep("a", workspace(tmp_path, "a"), now=0, hold=True)
    kept.start_clock("a", 100)
    kept.start_clock("a", 200)
    assert kept.workspace("a", now=111) is None


def test_held_slots_fill_up(tmp_path):
    kept = Retained(keep_seconds=10, max_retained=2)
    assert kept.full is False
    kept.keep("a", workspace(tmp_path, "a"), now=0, hold=True)
    assert kept.full is False
    kept.keep("b", workspace(tmp_path, "b"), now=0, hold=True)
    assert kept.full is True


def test_a_full_node_answers_503(tmp_path, monkeypatch):
    kept = Retained(keep_seconds=10, max_retained=1)
    kept.keep("a", workspace(tmp_path, "a"), now=0, hold=True)
    monkeypatch.setattr(main, "RETAINED", kept)
    with pytest.raises(main.HTTPException) as refused:
        main._admit_run(object())
    assert refused.value.status_code == 503


def seeded_run(engine, run_id="run_1", state="awaiting_review", **fields):
    with db.writing(engine) as session:
        version = repo.study_version_named(session, "st-01", 1) or add_version(session)
        repo.add_request(session, request_id="req_" + run_id,
                         study_id=version.study_id, study_version=1,
                         study_digest=version.digest, workflow_digest="sha256:w",
                         member="m-01", node="n", datasets=[], not_after=NOW,
                         payload_digest="sha256:" + run_id, raw=b"{}", now=NOW)
        repo.set_verdict(session, "req_" + run_id, Verdict.ACCEPTED)
        repo.save_run(session, run_id, state="COMPLETE", request_id="req_" + run_id,
                      ended_at=NOW, **fields)
        for stage in {"awaiting_review": ["rules_pending", "awaiting_review"],
                      "sent": ["rules_pending", "awaiting_review", "released", "sent"],
                      }[state]:
            repo.set_egress_state(session, run_id, EgressState(stage), NOW,
                                  retain_seconds=3600)


def test_retention_survives_a_restart(engine, tmp_path):
    held = workspace(tmp_path, "held")
    aging = workspace(tmp_path, "aging")
    seeded_run(engine, "run_1", "awaiting_review", workspace_path=held.path)
    seeded_run(engine, "run_2", "sent", workspace_path=aging.path)
    kept = Retained(keep_seconds=3600, max_retained=4)
    with db.reading(engine) as session:
        count = runtime.rebuild_retention(session, kept, Workspace, 3600,
                                          now=NOW + timedelta(minutes=1))
    assert count == 2
    far = (NOW + timedelta(days=30)).timestamp()
    assert kept.workspace("run_1", now=far) is not None
    assert kept.workspace("run_2", now=far) is None


def test_a_clock_already_running_keeps_what_was_left(engine, tmp_path):
    seeded_run(engine, "run_2", "sent", workspace_path=workspace(tmp_path, "a").path)
    kept = Retained(keep_seconds=3600, max_retained=4)
    with db.reading(engine) as session:
        runtime.rebuild_retention(session, kept, Workspace, 3600, now=NOW)
    soon = (NOW + timedelta(minutes=30)).timestamp()
    assert kept.workspace("run_2", now=soon) is not None


def test_a_restart_does_not_give_a_run_a_new_clock(engine, tmp_path):
    seeded_run(engine, "run_2", "sent", workspace_path=workspace(tmp_path, "a").path)
    kept = Retained(keep_seconds=3600, max_retained=4)
    with db.reading(engine) as session:
        runtime.rebuild_retention(session, kept, Workspace, 3600,
                                  now=NOW + timedelta(minutes=50))
    late = (NOW + timedelta(minutes=61)).timestamp()
    assert kept.workspace("run_2", now=late) is None


def test_a_workspace_that_vanished_is_skipped(engine, tmp_path):
    seeded_run(engine, "run_1", "awaiting_review",
               workspace_path=str(tmp_path / "gone"))
    kept = Retained(keep_seconds=3600, max_retained=4)
    with db.reading(engine) as session:
        assert runtime.rebuild_retention(session, kept, Workspace, 3600, now=NOW) == 0


def test_a_run_lost_in_a_restart_stops_claiming_to_run(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_9", state="RUNNING")
        runtime.settle_lost_runs(session, now=NOW)
    assert row_of(engine, "run_9").state == "SYSTEM_ERROR"


# The worker


def worker(engine, cancelled=None, **options):
    return runtime.NodeWorker(engine, cancel=cancelled.append if cancelled is not None else print,
                              retained=Retained(keep_seconds=60, max_retained=4),
                              keys=KEYS, node="n", **options)


def test_a_cancellation_from_the_page_reaches_the_api_process(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="RUNNING")
        repo.request_cancel(session, "run_1")
    cancelled = []
    worker(engine, cancelled).tick()
    assert cancelled == ["run_1"]


def test_cancelling_uses_the_existing_logic(monkeypatch):
    runs = RunStore()
    monkeypatch.setattr(main, "RUNS", runs)
    run = runs.create()
    main.cancel_from_store(run.run_id)
    assert run.state is RunState.CANCELING


def test_cancelling_a_finished_or_unknown_run_is_nothing(monkeypatch):
    runs = RunStore()
    monkeypatch.setattr(main, "RUNS", runs)
    main.cancel_from_store("nobody")
    run = runs.create()
    runs.advance(run.run_id, RunState.CANCELING)
    runs.advance(run.run_id, RunState.CANCELED)
    main.cancel_from_store(run.run_id)
    assert run.state is RunState.CANCELED


def ready_run(engine, tmp_path):
    seeded = {"workspace_path": str(tmp_path),
              "egress_files": [{"file": "a.tsv", "size": 1, "verdict": None,
                                "reason": None}]}
    with db.writing(engine) as session:
        version = add_version(session)
        repo.add_request(session, request_id="req_1", study_id=version.study_id,
                         study_version=1, study_digest=version.digest,
                         workflow_digest="sha256:w", member="m-01", node="n",
                         datasets=[], not_after=NOW, payload_digest="sha256:p",
                         raw=b"{}", now=NOW)
        repo.set_verdict(session, "req_1", Verdict.ACCEPTED)
        repo.save_run(session, "run_1", state="COMPLETE", request_id="req_1", **seeded)


def test_with_no_policy_engine_a_run_waits(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "manual")
    ready_run(engine, tmp_path)
    worker(engine).tick()
    assert row_of(engine, "run_1").egress_state == "not_ready"


def test_with_no_airlock_a_run_waits(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "airlock")
    ready_run(engine, tmp_path)
    worker(engine, policy=FakePolicyEngine()).tick()
    assert row_of(engine, "run_1").egress_state == "not_ready"


def test_the_worker_takes_a_run_through_the_rules(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "airlock")
    ready_run(engine, tmp_path)
    airlock = egress.FakeAirlock()
    worker(engine, policy=FakePolicyEngine(), airlock=airlock).tick()
    assert row_of(engine, "run_1").egress_state == "awaiting_airlock"


def test_a_run_with_no_request_has_no_egress(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "manual")
    with db.writing(engine) as session:
        repo.save_run(session, "run_1", state="COMPLETE", workspace_path=str(tmp_path))
    worker(engine, policy=FakePolicyEngine()).tick()
    assert row_of(engine, "run_1").egress_state == "not_ready"


def test_the_worker_starts_the_clock_after_egress(engine, tmp_path, monkeypatch):
    seeded_run(engine, "run_1", "sent", workspace_path=workspace(tmp_path, "a").path)
    chosen = worker(engine, keep_seconds=3600)
    ws = workspace(tmp_path, "b")
    chosen.retained.keep("run_1", ws, now=0, hold=True)
    chosen.tick()
    far = (NOW + timedelta(days=3)).timestamp()
    assert chosen.retained.workspace("run_1", now=far) is None


@pytest.mark.parametrize("value,expected", [("", None), ("manual", "manual"),
                                            ("airlock", "airlock")])
def test_the_mode_setting(monkeypatch, value, expected):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", value)
    assert runtime.mode() == expected


def test_a_wrong_mode_is_refused(monkeypatch):
    monkeypatch.setenv("BIOCHEF_EGRESS_MODE", "both")
    with pytest.raises(ValueError, match="BIOCHEF_EGRESS_MODE"):
        runtime.mode()


def test_a_plain_agent_starts_no_worker(monkeypatch):
    monkeypatch.delenv("BIOCHEF_EGRESS_MODE", raising=False)
    assert main.start_node_worker() is None
