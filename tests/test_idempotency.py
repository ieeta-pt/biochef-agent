"""A hub that retries a submission runs the work twice (#87).

Recorded before the fix. Every assertion states something true of the service
today and is meant to be deleted or inverted by the implementing commit.

A hub retries when a submission times out, when a proxy drops the response, or
when it restarts mid-flight and replays its queue. Today each retry is a fresh
run: the tool executes again, a second execution slot is taken, and a second
record is retained. So a hub retrying under load pushes the agent further into
the 503 that made it retry.

This is the other half of #84. That one lets a hub recognise an orphaned run
after the fact; it does nothing to stop the retry creating one.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras_mod = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

    client_mod.OrasClient = _Client
    oras_mod.client = client_mod
    sys.modules["oras"] = oras_mod
    sys.modules["oras.client"] = client_mod


import main
from runs import RunStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_async_runs import WORKFLOW, _poll, _submit, service  # noqa: F401

KEY = "hub-submission-0001"


def test_nothing_in_the_tree_knows_what_an_idempotency_key_is():
    joined = "\n".join(p.read_text() for p in REPO_ROOT.glob("*.py")).lower()
    for spelling in ("idempotency", "idempotent", "fingerprint", "replay"):
        assert spelling not in joined, f"{spelling!r} appears already"


def test_the_same_submission_twice_runs_the_work_twice(service, monkeypatch):
    """The measurement in the issue, as a test.

    Not just two run records: the tool itself executes twice, which is the
    cost a hub cannot currently avoid.
    """
    from fastapi.testclient import TestClient

    executions = []
    original = main.run_snakemake

    def counting(ws, timeout_s=None, **kwargs):
        executions.append(1)
        return original(ws, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(main, "run_snakemake", counting)

    with TestClient(main.app) as client:
        first = _submit(client).json()
        second = _submit(client).json()
        assert first["run_id"] != second["run_id"], "two separate runs"
        assert _poll(client, first["run_id"])["state"] == "COMPLETE"
        assert _poll(client, second["run_id"])["state"] == "COMPLETE"

    assert len(executions) == 2, (
        "one logical submission, executed twice -- this is the defect"
    )


def test_a_key_the_caller_offers_is_ignored(service):
    """So a hub cannot opt into protection even if it asks for it."""
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        def submit():
            return client.post(
                "/runs",
                data={"biochef_workflow": WORKFLOW},
                files=[("files", ("input-1-out", b"in",
                                  "application/octet-stream"))],
                headers={"Idempotency-Key": KEY},
            )

        first, second = submit(), submit()
        assert first.status_code == second.status_code == 202
        assert first.json()["run_id"] != second.json()["run_id"]


def test_both_duplicates_take_a_slot_and_a_retained_record(service):
    """The pressure a retry adds is on both bounds, not only on compute."""
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        before = len(main.RUNS._runs)
        _submit(client)
        _submit(client)
        assert len(main.RUNS._runs) == before + 2


def test_a_run_cannot_say_which_key_asked_for_it():
    run = RunStore().create()
    assert "idempotency_key" not in run.as_dict()
    assert not hasattr(run, "idempotency_key")


def test_the_store_has_no_way_to_find_a_run_by_anything_but_its_id():
    store = RunStore()
    assert not hasattr(store, "lookup")
    assert not hasattr(store, "by_key")
