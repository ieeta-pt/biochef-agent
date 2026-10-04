"""Recognising a submission this agent has already accepted (#87).

A hub retries when a submission times out, when a proxy drops the response, or
when it restarts mid-flight and replays its queue. Today each retry is a fresh
run: the tool executes again, a second execution slot is taken, and a second
record is retained. So a hub retrying under load pushes the agent further into
the 503 that made it retry.

This is the other half of #84. That one lets a hub recognise an orphaned run
after the fact; this stops the retry creating one.

Two behaviours here are deliberately the opposite of #84's, and most of the
care is in the cases where a key must NOT be remembered: a refusal, and a
submission whose body never arrived. A key burnt by something that did not
happen turns a transient failure into a permanent one.
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


import pytest

import idempotency
import main
from runs import KeyInFlight, Replay, RunCapacityError, RunState, RunStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_async_runs import WORKFLOW, _poll, _submit, service  # noqa: F401

KEY = "hub-submission-0001"


def _settle(store, run):
    """Drive a run to COMPLETE, which is what makes it evictable."""
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.RUNNING)
    store.advance(run.run_id, RunState.COMPLETE)


def _post(client, key=KEY, payload=b"in", workflow=None):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        "/runs",
        data={"biochef_workflow": workflow or WORKFLOW},
        files=[("files", ("input-1-out", payload, "application/octet-stream"))],
        headers=headers,
    )


@pytest.fixture
def counted(monkeypatch):
    """A client, and how many times the runner was actually entered."""
    from fastapi.testclient import TestClient

    executions = []
    original = main.run_snakemake

    def counting(ws, timeout_s=None, **kwargs):
        executions.append(1)
        return original(ws, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(main, "run_snakemake", counting)
    with TestClient(main.app) as client:
        yield client, executions


# --- the point of the whole thing -------------------------------------------

def test_a_retried_submission_runs_the_work_once(service, counted):
    client, executions = counted

    first = _post(client)
    replay = _post(client)

    assert first.status_code == 202
    assert replay.status_code == 200, (
        "200, not 202: nothing was accepted, and a hub reading its own log "
        "should be able to tell a replay from an acceptance"
    )
    assert replay.json()["run_id"] == first.json()["run_id"]

    _poll(client, first.json()["run_id"])
    assert len(executions) == 1, "the tool ran once for one logical submission"


def test_a_replay_does_not_take_a_second_slot_or_record(service, counted):
    client, _ = counted
    _post(client)
    retained = len(main.RUNS._runs)
    for _ in range(5):
        _post(client)
    assert len(main.RUNS._runs) == retained, (
        "retrying under load must not add to the pressure that caused it"
    )


def test_a_replay_works_after_the_run_has_finished(service, counted):
    client, executions = counted
    first = _post(client)
    assert _poll(client, first.json()["run_id"])["state"] == "COMPLETE"

    replay = _post(client)
    assert replay.status_code == 200
    assert replay.json()["state"] == "COMPLETE"
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert len(executions) == 1


def test_without_a_key_nothing_changes(service, counted):
    """Protection is opt-in. A caller that does not ask does not get it, and
    the old behaviour is what every existing client depends on."""
    client, executions = counted
    first, second = _post(client, key=None), _post(client, key=None)
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] != second.json()["run_id"]
    assert "idempotency_key" not in first.json()
    _poll(client, first.json()["run_id"])
    _poll(client, second.json()["run_id"])
    assert len(executions) == 2


def test_the_run_says_which_key_created_it(service, counted):
    """So a caller holding a key can confirm the run it got back is the one
    that key created, rather than taking it on trust."""
    client, _ = counted
    run_id = _post(client).json()["run_id"]
    assert _post(client).json()["idempotency_key"] == KEY
    polled = client.get(f"/runs/{run_id}")
    assert polled.json()["idempotency_key"] == KEY


# --- the same key used for something else -----------------------------------

@pytest.mark.parametrize("difference", [
    {"payload": b"different bytes"},
    {"workflow": WORKFLOW.replace("tool-1", "tool-2")},
])
def test_the_same_key_for_a_different_submission_is_refused(
        service, counted, difference):
    """Returning the first run would execute neither this workflow nor report
    that it was dropped, which is the worse of the two mistakes."""
    client, executions = counted
    _post(client)
    conflicting = _post(client, **difference)
    assert conflicting.status_code == 422
    assert "different submission" in conflicting.json()["detail"]
    assert len(executions) <= 1


def test_a_replay_is_judged_on_the_body_and_not_on_the_key_alone(service, counted):
    """Which means a replay reads the uploads. It has to: whether this is the
    same submission cannot be known without looking at it."""
    client, _ = counted
    _post(client)
    assert _post(client, payload=b"in").status_code == 200
    assert _post(client, payload=b"in ").status_code == 422


def test_the_order_parts_arrive_in_does_not_make_it_a_different_submission():
    """Multipart ordering is not something a retrying client controls."""
    one = idempotency.fingerprint("w", [("a", b"1"), ("b", b"2")])
    other = idempotency.fingerprint("w", [("b", b"2"), ("a", b"1")])
    assert one == other


def test_two_different_submissions_cannot_fingerprint_the_same():
    """Length-prefixed, not joined on a separator.

    A file named `a` holding `bc` against one named `ab` holding `c` would
    hash identically under naive concatenation, and this value decides whether
    work is skipped.
    """
    assert (idempotency.fingerprint("w", [("a", b"bc")])
            != idempotency.fingerprint("w", [("ab", b"c")]))
    assert (idempotency.fingerprint("ab", [])
            != idempotency.fingerprint("a", [("", b"b")]))


# --- an unusable key is refused, not ignored --------------------------------

@pytest.mark.parametrize("unusable", [
    "", "a b", "a\tb", "a\x00b", "\x7f", "a" * (idempotency.MAX_LENGTH + 1),
])
def test_an_unusable_key_refuses_the_request(service, counted, unusable):
    """The opposite of what #84 does with a bad X-Request-Id, deliberately.

    Ignoring a bad request id costs a lost correlation. Ignoring a bad
    idempotency key costs the caller the exact protection it asked for: it
    believes a retry is safe, retries, and the work runs twice. Silently
    degrading a safety guarantee is worse than refusing.
    """
    client, executions = counted
    refused = _post(client, key=unusable)
    assert refused.status_code == 400
    assert not executions, "nothing ran, so it is safe to retry"
    assert "safe to retry" in refused.json()["detail"]


def test_a_refused_key_is_not_remembered(service, counted):
    """Or the caller could never use that key again, even corrected."""
    client, _ = counted
    _post(client, key="a b")
    assert main.RUNS._by_key == {}


def test_the_key_is_not_echoed_in_a_header(service, counted):
    """It identifies a submission rather than describing a response, and
    putting it on the way out would make it a second value to keep legal for
    the wire -- see #84 for what that costs."""
    client, _ = counted
    assert _post(client).headers.get("idempotency-key") is None


# --- the cases where a key must NOT be burnt --------------------------------

def test_a_refusal_at_capacity_does_not_burn_the_key(service, counted):
    """A transient refusal must not become a permanent one.

    The 503 happens before any key is recorded, so the same key retried once a
    slot frees up creates the run it was always meant to.
    """
    client, _ = counted
    store = RunStore(max_runs=1)
    blocking = store.create()
    store.advance(blocking.run_id, RunState.INITIALIZING)
    store.advance(blocking.run_id, RunState.RUNNING)
    main.RUNS = store

    refused = _post(client)
    assert refused.status_code == 503
    assert store._by_key == {}, "nothing happened, so the key is still free"

    store.advance(blocking.run_id, RunState.COMPLETE)
    accepted = _post(client)
    assert accepted.status_code == 202, (
        "the same key must work once there is room"
    )
    assert accepted.json()["idempotency_key"] == KEY


def test_a_submission_whose_body_never_arrived_does_not_burn_the_key():
    """The upload-read failure path already undoes the admission; it has to
    undo the key with it, or every retry is told the work is already done."""
    import asyncio

    from test_async_runs import _bare_request

    class Broken:
        filename = "input-1-out"
        size = 4

        async def read(self):
            raise OSError("input stream failed")

    store = RunStore()
    main.RUNS = store
    with pytest.raises(OSError):
        asyncio.run(main.submit_run(_bare_request(), WORKFLOW, [Broken()],
                                    idempotency_key=KEY))
    assert store._by_key == {}
    assert len(store._runs) == 0


def test_a_key_whose_run_was_evicted_can_create_a_new_one():
    """The window a key is honoured for is the retention window, and no
    longer: the index entry dies with the run it points at.

    Duplicating work long after the original finished and aged out is the
    price of not keeping a second unbounded store of keys, and it is a
    documented bound rather than a surprise.
    """
    store = RunStore(max_runs=2)
    first = store.create(idempotency_key=KEY)
    store.settle_fingerprint(first.run_id, "fp")
    _settle(store, first)

    with pytest.raises(Replay):
        store.create(idempotency_key=KEY)

    # Two fillers, not one: eviction fires on the create that would exceed the
    # cap, so with max_runs=2 the second filler is what displaces the oldest
    # terminal run -- and that is the keyed one.
    for _ in range(2):
        _settle(store, store.create())
    assert first.run_id not in store._runs, (
        "the keyed run was the oldest terminal one, so it went first"
    )

    fresh = store.create(idempotency_key=KEY)
    assert fresh.run_id != first.run_id
    assert store._by_key[KEY] == fresh.run_id


def test_discarding_a_stale_run_does_not_steal_a_live_run_s_key():
    """_forget_key checks ownership rather than assuming it.

    Once a key has been reassigned to a newer run, dropping the older run must
    not delete the index entry the newer one now owns.
    """
    store = RunStore(max_runs=2)
    stale = store.create(idempotency_key=KEY)
    store.settle_fingerprint(stale.run_id, "fp")
    _settle(store, stale)
    for _ in range(2):
        _settle(store, store.create())
    assert stale.run_id not in store._runs
    fresh = store.create(idempotency_key=KEY)

    store._forget_key(stale)                          # the stale record again
    assert store._by_key.get(KEY) == fresh.run_id


def test_the_index_never_holds_more_than_the_store_does():
    store = RunStore(max_runs=4)
    for i in range(40):
        run = store.create(idempotency_key=f"key-{i}")
        store.settle_fingerprint(run.run_id, "fp")
        _settle(store, run)
    assert len(store._by_key) <= len(store._runs) <= 4
    assert all(run_id in store._runs for run_id in store._by_key.values()), (
        "every index entry points at a run that still exists"
    )


# --- two retries racing, which is what retrying looks like ------------------

def test_the_lookup_and_the_creation_are_one_critical_section():
    """The guard the whole design rests on, tested deterministically.

    Two concurrent retries must not both create a run, and checking first then
    creating after would have both find nothing and both create. This is the
    likeliest shape for a retry to arrive in: a client that timed out and a
    client that gave up and resent.

    Counted rather than raced. The hammering test below is a safety net, but it
    is only probabilistic -- against a version that splits the lock it lost the
    race in 2 rounds out of 60, so as the primary guard it would miss a
    regression most of the time. Counting critical sections is exact: one
    keyed create() enters the lock once, and any version that reads the index
    and then creates separately enters it twice.

    The lock is instrumented by wrapping the one the store was handed, so
    nothing in the store changes to be measured.
    """
    import threading

    class Counting:
        def __init__(self):
            self.inner = threading.Lock()
            self.entries = 0

        def __enter__(self):
            self.entries += 1
            return self.inner.__enter__()

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    store = RunStore()
    lock = Counting()
    store._lock = lock

    lock.entries = 0
    store.create(idempotency_key=KEY)
    assert lock.entries == 1, (
        f"a keyed create() entered the lock {lock.entries} times; the index "
        f"lookup and the creation must be one critical section or two retries "
        f"can both pass the lookup"
    )


def test_concurrent_retries_do_not_both_create_a_run():
    """Corroborates the test above by actually racing it.

    A safety net rather than the guard: CPython switches threads every 5ms by
    default, so the window this is trying to hit is usually not hit at all.
    The switch interval is lowered to make the scheduler adversarial, which
    found 2 duplicating rounds out of 60 against a deliberately split lock and
    none against this one -- so it can fail on a regression but cannot fail on
    correct code, which is the right direction for a test like this.
    """
    import threading

    store = RunStore(max_runs=4096)
    original_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for round_number in range(40):
            key = f"{KEY}-{round_number}"
            created, refused = [], []
            start = threading.Barrier(24)

            def retry():
                start.wait()
                try:
                    created.append(store.create(idempotency_key=key).run_id)
                except (Replay, KeyInFlight):
                    refused.append(1)

            threads = [threading.Thread(target=retry) for _ in range(24)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert len(created) == 1, (
                f"round {round_number}: {len(created)} runs created for one key"
            )
            assert len(refused) == 23
            assert store._by_key[key] == created[0]
    finally:
        sys.setswitchinterval(original_interval)


def test_a_racing_retry_is_told_to_wait_rather_than_given_an_unknown_run():
    """Until the body has been read the agent cannot say whether the second
    request is the same submission, and handing back a run whose fingerprint
    is unset would answer a question it has not checked."""
    store = RunStore()
    first = store.create(idempotency_key=KEY)
    assert first.fingerprint is None

    with pytest.raises(KeyInFlight):
        store.create(idempotency_key=KEY)

    store.settle_fingerprint(first.run_id, "fp")
    with pytest.raises(Replay) as settled:
        store.create(idempotency_key=KEY)
    assert settled.value.run.run_id == first.run_id


def test_a_racing_retry_over_http_gets_409_and_not_a_second_run(
        service, counted, monkeypatch):
    client, executions = counted

    store = RunStore()
    reserved = store.create(idempotency_key=KEY)
    monkeypatch.setattr(main, "RUNS", store)

    racing = _post(client)
    assert racing.status_code == 409
    assert racing.headers["Retry-After"]
    assert len(store._runs) == 1, "no second run"
    assert not executions

    store.settle_fingerprint(reserved.run_id,
                             idempotency.fingerprint(WORKFLOW,
                                                     [("input-1-out", b"in")]))
    assert _post(client).status_code == 200
