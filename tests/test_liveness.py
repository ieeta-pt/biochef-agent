"""Whether this agent can say it is alive, and how busy it is (#82).

A hub orchestrating several sites needs two answers and they are different
questions with different audiences.

A liveness probe is run by an orchestrator, not a person, and carries no
credentials. An endpoint that demands a token turns a mistyped token into a
healthy service that looks dead and is restarted forever. So liveness answers
unauthenticated, and therefore must say nothing whatever beyond "up".

Capacity is the opposite. Free slots, runner and provider describe the
deployment, so they sit behind authentication. Before this, the only way the hub
could discover saturation was to submit work and be refused with 503, which is
finding out after sending it to the wrong site.

Most of what follows is about the unauthenticated path, because that is the
whole risk of this change.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import main
from runs import RunCapacityError, RunState, RunStore

TOKEN = "a-shared-secret"


@pytest.fixture
def client(monkeypatch):
    """The real routes, behind bearer authentication actually in place.

    Exercising these against the default `none` provider would prove nothing:
    every path answers when nothing is checked, so the exemption would look
    correct while being entirely untested.

    main.AUTH is patched to the same provider the middleware enforces with,
    which is how production wires it -- app.add_middleware(..., provider=AUTH).
    A separate test pins that they are the same object.
    """
    provider = auth.BearerAuth(TOKEN)
    monkeypatch.setattr(main, "AUTH", provider)

    app = FastAPI()
    for route in main.app.routes:
        if getattr(route, "path", None) in ("/health", "/capacity"):
            app.router.routes.append(route)
    app.add_middleware(auth.AuthenticationMiddleware, provider=provider)
    return TestClient(app)


def authorised():
    return {"Authorization": f"Bearer {TOKEN}"}


# --- liveness, and what it refuses to say -----------------------------------

def test_health_answers_without_credentials(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_head_probe_is_not_refused(client):
    """starlette answers HEAD on every GET route, so a HEAD probe reaches a path
    that exists. Exempting only GET would refuse it, and a HEAD carries no body
    back, so there is nothing to withhold."""
    assert client.head("/health").status_code == 200


def test_health_says_nothing_but_up(client):
    """Anything else here is published to whatever can reach the port."""
    body = client.get("/health").json()
    assert set(body) == {"status"}
    serialised = client.get("/health").text
    for leaked in ("runner", "slot", "version", "bearer", "apptainer",
                   "subprocess", "queued", "capacity"):
        assert leaked not in serialised.lower(), f"liveness leaks {leaked!r}"


# --- the exemption surface --------------------------------------------------

def test_the_exemption_does_not_extend_to_other_methods(client):
    """Exempting a path must not exempt what can be done to it."""
    for call in (client.post, client.put, client.delete, client.patch):
        assert call("/health").status_code == 401


def test_the_exemption_does_not_extend_to_neighbouring_paths(client):
    """Matched exactly, never as a prefix, so /health is not a door."""
    for path in ("/health/", "/healthz", "/health/x", "/Health", "/HEALTH",
                 "/health%20", " /health"):
        assert client.get(path).status_code == 401, path


def test_percent_encoding_cannot_slip_past_the_match(client):
    """scope["path"] arrives decoded, which is also what the router matches, so
    the comparison is against the same string routing sees. /he%61lth decodes to
    /health and is therefore legitimately liveness, while an encoded traversal
    decodes to a path that simply is not in the set."""
    assert client.get("/he%61lth").status_code == 200
    for sneaky in ("/health%2F..%2Fcapacity", "/capacity%00/health"):
        assert client.get(sneaky).status_code == 401, sneaky


def test_a_traversal_does_not_reach_capacity_unauthenticated(client):
    assert client.get("/health/../capacity").status_code == 401


def test_the_exemption_is_two_entries_and_both_are_liveness():
    """Pinned so an addition is a decision rather than a drift. Anything in this
    set answers to whatever can reach the port."""
    assert auth.AuthenticationMiddleware.OPEN == frozenset(
        {("GET", "/health"), ("HEAD", "/health")})


def test_the_exemption_is_immutable():
    """A set a caller can add to at runtime is not a reviewable surface."""
    assert isinstance(auth.AuthenticationMiddleware.OPEN, frozenset)


def test_skipping_authentication_does_not_skip_the_rest_of_the_stack():
    """Measured, not read: an oversized body on the exempt path is still 413.

    The exemption passes the request inward instead of answering it, so
    everything below the auth layer still applies. An exemption that
    short-circuited would hand the one unauthenticated route an unbounded body,
    which is the opposite of what an exemption should cost.

    The order mirrors main.py: BodySizeLimitMiddleware is added first and is
    therefore inner, AuthenticationMiddleware is added last and is outer.
    """
    import bodylimit

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.add_middleware(bodylimit.BodySizeLimitMiddleware, max_bytes=8)
    app.add_middleware(auth.AuthenticationMiddleware,
                       provider=auth.BearerAuth(TOKEN))
    probe = TestClient(app)

    assert probe.get("/health").status_code == 200
    oversized = probe.request("GET", "/health", content=b"x" * 64)
    assert oversized.status_code == 413, (
        "the body limit no longer applies to the unauthenticated route"
    )


def test_the_order_of_the_two_middlewares_is_the_one_measured_above():
    """So the test above keeps describing production.

    Swapping the two would put the body limit outside authentication, where it
    would also bound bodies that are about to be refused anyway -- harmless --
    but would mean the exempt path is no longer the one measured. Pinned on the
    stripped source, because grep matches a comment as readily as code.
    """
    import re

    source = (REPO_ROOT / "main.py").read_text()
    code = re.sub(r"#.*", "", source)
    limit = code.index("add_middleware(BodySizeLimitMiddleware")
    authn = code.index("add_middleware(AuthenticationMiddleware")
    assert limit < authn, (
        "authentication must be added last so it is the outermost layer"
    )


# --- capacity ---------------------------------------------------------------

def test_capacity_needs_credentials(client):
    refused = client.get("/capacity")
    assert refused.status_code == 401
    assert refused.headers.get("www-authenticate") == "Bearer"


def test_capacity_reports_what_a_hub_routes_on(client):
    body = client.get("/capacity", headers=authorised()).json()
    assert body["runner"] in ("subprocess", "apptainer")
    assert body["authentication"] == "bearer"
    assert body["slots"]["total"] >= 1
    assert body["slots"]["busy"] + body["slots"]["free"] == body["slots"]["total"]
    assert 0 <= body["slots"]["free"] <= body["slots"]["total"]
    assert body["retained"]["cap"] >= 1


def test_capacity_reports_the_provider_the_stack_enforces():
    """Not a second reading of the environment. A field saying `bearer` while
    the stack admits anyone is worse than no field at all, so production has to
    hand the middleware the same object this endpoint reports."""
    source = (REPO_ROOT / "main.py").read_text()
    assert "app.add_middleware(AuthenticationMiddleware, provider=AUTH)" in source
    assert '"authentication": AUTH.name' in source


def test_an_undeclared_version_is_null_and_not_invented(client):
    """A number this service made up would be worse than none, because a hub
    routing work would believe it."""
    assert client.get("/capacity", headers=authorised()).json()["version"] is None


def test_a_version_of_nothing_but_whitespace_is_not_a_version():
    """Which is what a CI template substituting an empty variable produces.

    The setting is read at import, so this reads it in a fresh interpreter
    rather than reloading the module in this one -- a reload replaces RUNS, AUTH
    and app while other tests in the session hold the old ones, and patching the
    constant afterwards would pass even if the value were never stripped where
    it is read.
    """
    import os
    import subprocess

    def version_for(value):
        env = dict(os.environ, BIOCHEF_AGENT_VERSION=value)
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, types\n"
             "m = types.ModuleType('oras'); c = types.ModuleType('oras.client')\n"
             "class C:\n"
             "    def __init__(s, *a, **k): pass\n"
             "    def login(s, *a, **k): pass\n"
             "c.OrasClient = C; m.client = c\n"
             "sys.modules['oras'] = m; sys.modules['oras.client'] = c\n"
             "import main; print(repr(main.AGENT_VERSION))"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        assert probe.returncode == 0, probe.stderr[-2000:]
        return eval(probe.stdout.strip())

    assert version_for("  \t ") == "", "whitespace is not a version"
    assert version_for("  2026.10-abc123 ") == "2026.10-abc123", (
        "a version is reported without the whitespace around it"
    )


def test_datasets_are_null_rather_than_an_empty_list(client):
    """Which datasets a site holds comes from the DataSource interface, which is
    not in this tree. [] would read as "this site holds none"."""
    assert client.get("/capacity", headers=authorised()).json()["datasets"] is None


# --- the counting behind it -------------------------------------------------

def test_queued_runs_are_not_counted_as_busy():
    """A QUEUED run is admitted and waiting, not occupying a slot. Counting it
    as busy would have a hub route away from an agent that is in fact free."""
    store = RunStore()
    waiting, running = store.create(), store.create()
    store.advance(running.run_id, RunState.INITIALIZING)
    store.advance(running.run_id, RunState.RUNNING)

    counts = store.state_counts()
    busy = sum(counts.get(s.value, 0) for s in main.BUSY_STATES)
    assert counts[RunState.QUEUED.value] == 1
    assert busy == 1, "a queued run was counted against the slots"


def test_canceling_still_holds_its_slot():
    """The worker has not let go of it yet."""
    assert RunState.CANCELING in main.BUSY_STATES


def test_a_finished_run_frees_its_slot():
    store = RunStore()
    run = store.create()
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.RUNNING)
    store.advance(run.run_id, RunState.COMPLETE)
    busy = sum(store.state_counts().get(s.value, 0) for s in main.BUSY_STATES)
    assert busy == 0


def test_the_counts_are_walked_and_not_tallied():
    """A tally drifts the first time an eviction is not accounted for, and a
    wrong count is harder to notice than a slow one."""
    store = RunStore(max_runs=2)
    first = store.create()
    store.advance(first.run_id, RunState.INITIALIZING)
    store.advance(first.run_id, RunState.RUNNING)
    store.advance(first.run_id, RunState.COMPLETE)
    store.create()
    store.create()
    assert store.retained() == 2
    assert sum(store.state_counts().values()) == 2, (
        "the counts disagree with what the store holds"
    )


def test_counting_is_safe_while_runs_are_admitted_and_evicted():
    """Requests arrive while a walk of the store is happening.

    The dangerous neighbour is admission, not advancement: create() inserts and
    may evict, which changes the dict's size mid-iteration -- a RuntimeError in
    whichever thread is unlucky. Advancing a run only changes a value, so a test
    that advances alone proves nothing, and the first version of this test
    passed with the lock removed.

    max_runs is small so eviction runs constantly, which is the deletion half.
    """
    import threading

    store = RunStore(max_runs=8)
    errors = []
    stop = threading.Event()

    def churn():
        try:
            while not stop.is_set():
                run = store.create()
                store.advance(run.run_id, RunState.INITIALIZING)
                store.advance(run.run_id, RunState.RUNNING)
                store.advance(run.run_id, RunState.COMPLETE)
        except Exception as exc:
            errors.append(exc)

    def count():
        try:
            for _ in range(4000):
                store.state_counts()
                store.retained()
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=churn, daemon=True)
    reader = threading.Thread(target=count)
    writer.start()
    reader.start()
    reader.join()
    stop.set()
    writer.join(timeout=5)
    assert not errors, errors


# --- occupancy, which is not the same as run records ------------------------

def test_a_synchronous_conversion_shows_as_a_busy_slot():
    """The defect this test was written for: /convert holds a slot for the whole
    of a synchronous conversion and never creates a run record, so a /capacity
    that counted run states reported `free: 4` on an agent whose every slot was
    busy -- the exact mistake the endpoint exists to stop a hub making.

    Measured by holding real slots through the real chokepoint, not by faking
    the count.
    """
    import asyncio

    async def exercise():
        main._occupied_by_loop.clear()
        main._slots_by_loop.clear()
        assert main.slots_busy() == 0

        held = []
        async with main._slots():
            held.append(main.slots_busy())
            async with main._slots():
                held.append(main.slots_busy())
            held.append(main.slots_busy())
        held.append(main.slots_busy())
        return held

    assert asyncio.run(exercise()) == [1, 2, 1, 0]


def test_a_slot_is_given_back_in_the_count_when_the_request_fails():
    """Released in a finally. A slot that stays counted after the holder raised
    makes the agent look permanently busier than it is, and nothing short of a
    restart corrects it."""
    import asyncio

    async def exercise():
        main._occupied_by_loop.clear()
        main._slots_by_loop.clear()
        try:
            async with main._slots():
                raise RuntimeError("the conversion failed")
        except RuntimeError:
            pass
        return main.slots_busy()

    assert asyncio.run(exercise()) == 0


def test_a_cancelled_holder_gives_its_slot_back_too():
    """Cancellation is a BaseException, not an Exception, and a cleanup written
    as `except Exception` would miss it."""
    import asyncio

    async def exercise():
        main._occupied_by_loop.clear()
        main._slots_by_loop.clear()

        async def holder(entered):
            async with main._slots():
                entered.set()
                await asyncio.sleep(3600)

        entered = asyncio.Event()
        task = asyncio.create_task(holder(entered))
        await entered.wait()
        assert main.slots_busy() == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return main.slots_busy()

    assert asyncio.run(exercise()) == 0


def test_the_free_count_can_never_be_reported_out_of_bounds():
    """A wrong free count is the one thing this endpoint must not produce, so
    the reading is clamped rather than trusted."""
    import asyncio

    async def exercise():
        loop = asyncio.get_running_loop()
        main._occupied_by_loop[loop] = -5
        below = main.slots_busy()
        main._occupied_by_loop[loop] = main.MAX_CONCURRENT_RUNS + 99
        above = main.slots_busy()
        main._occupied_by_loop.clear()
        return below, above

    below, above = asyncio.run(exercise())
    assert below == 0
    assert above == main.MAX_CONCURRENT_RUNS


def test_capacity_sees_a_held_slot_that_has_no_run_record(monkeypatch):
    """The one that pins the fix.

    A slot is held the way /convert holds one -- through the real chokepoint,
    with no run record behind it -- and /capacity is asked while it is held, on
    the same loop. An endpoint that derived occupancy from run states answers
    `busy: 0, free: total` here, because there is nothing in the store to
    count, which is how the defect got as far as a pushed branch.

    The earlier version of this test only asserted busy >= in_flight, and that
    holds trivially when both are zero; the mutation reverting occupancy to run
    states passed against it.
    """
    import asyncio

    import httpx

    provider = auth.BearerAuth(TOKEN)
    monkeypatch.setattr(main, "AUTH", provider)

    app = FastAPI()
    for route in main.app.routes:
        if getattr(route, "path", None) == "/capacity":
            app.router.routes.append(route)
    app.add_middleware(auth.AuthenticationMiddleware, provider=provider)

    async def exercise():
        main._occupied_by_loop.clear()
        main._slots_by_loop.clear()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://agent") as remote:
            idle = (await remote.get("/capacity", headers=authorised())).json()
            async with main._slots():
                held = (await remote.get("/capacity",
                                         headers=authorised())).json()
        return idle, held

    idle, held = asyncio.run(exercise())
    total = idle["slots"]["total"]

    assert idle["slots"] == {"total": total, "busy": 0, "free": total}
    assert held["slots"] == {"total": total, "busy": 1, "free": total - 1}, (
        "a held slot with no run record was reported as free"
    )
    assert held["runs"]["in_flight"] == 0, (
        "nothing in the run store, which is the whole point"
    )
    assert held["runs"]["by_state"] == {}
    assert "slots" not in held["runs"], (
        "slots moved out of runs, because they are not only runs"
    )


# --- whether work would be taken at all -------------------------------------

def _finish(store, run):
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.RUNNING)
    store.advance(run.run_id, RunState.COMPLETE)


def _start(store, run):
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.RUNNING)


def _at_the_cap(terminal=0, running=0):
    """A store with MAX_RUNS records in the states asked for."""
    store = RunStore(max_runs=3)
    runs = [store.create() for _ in range(3)]
    for run in runs[:terminal]:
        _finish(store, run)
    for run in runs[terminal:terminal + running]:
        _start(store, run)
    return store


@pytest.mark.parametrize("label, store", [
    ("empty", RunStore(max_runs=3)),
    ("under the cap", None),
    ("at the cap, all queued", _at_the_cap()),
    ("at the cap, one terminal", _at_the_cap(terminal=1)),
    ("at the cap, all running", _at_the_cap(running=3)),
])
def test_accepting_agrees_with_what_submitting_actually_does(label, store):
    """Compared against the behaviour, not against a second copy of the rule.

    accepting() restates _evict_if_needed's condition, and a restatement drifts.
    Asserting it against another spelling of the same condition would drift with
    it, so each case asks the predicate and then actually submits.
    """
    if store is None:
        store = RunStore(max_runs=3)
        store.create()
        store.create()

    predicted = store.accepting()
    try:
        store.create()
        admitted = True
    except RunCapacityError:
        admitted = False

    assert predicted == admitted, (
        f"{label}: accepting() said {predicted}, submitting gave {admitted}"
    )


def test_an_agent_can_be_entirely_free_and_still_refuse_everything():
    """Which is why `accepting` exists and free slots do not answer it.

    MAX_RUNS runs admitted and queued, nothing executing: every slot idle, and
    the next submission is refused with 503.
    """
    store = _at_the_cap()
    counts = store.state_counts()
    busy = sum(counts.get(s.value, 0) for s in main.BUSY_STATES)

    assert busy == 0, "nothing is executing"
    assert store.accepting() is False, "and yet no further work can be admitted"


def test_capacity_says_whether_it_is_accepting(client):
    body = client.get("/capacity", headers=authorised()).json()
    assert body["runs"]["accepting"] is True


def test_capacity_says_so_when_it_is_not_accepting(client, monkeypatch):
    monkeypatch.setattr(main, "RUNS", _at_the_cap())
    body = client.get("/capacity", headers=authorised()).json()
    assert body["runs"]["accepting"] is False
    assert body["slots"]["free"] == body["slots"]["total"], (
        "the slots really are idle, which is the point"
    )
