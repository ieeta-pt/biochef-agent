"""Tying one of a hub's requests to what this agent did with it (#84).

The gap this closes is worst on the refusals. POST /runs answers 503 when run capacity is
reached and 401 when credentials are wrong, and both carry nothing but a detail
string -- no run_id, because no run was created. A hub that submitted ten
workflows and got three refusals cannot say which three without matching on the
bodies it sent. Same for a 413 from the body limit and a 422 from a malformed
workflow.

On success it is only slightly better: the run_id is one the agent invented, so
if the response is lost in transit the run is orphaned -- executing here,
unknown there.

Most of what follows is about the inbound value, because that is the whole risk:
it is caller-supplied and gets written into a response header. The failure to
guard against is not injection -- h11, which uvicorn speaks, refuses to write an
illegal value -- but something worse in practice, an otherwise-fine response
destroyed by LocalProtocolError over a cosmetic header. A caller could delete
its own successful submissions.

TestClient cannot show either failure: it does not go over the wire and carries
a CRLF value through untouched, so a test that echoed a hostile id and looked
for an injected header would pass against an implementation validating nothing.
The tests below therefore check what this service EMITS against h11 itself,
rather than against the pattern used to accept it -- a pattern I wrote agreeing
with an implementation I wrote proves nothing about a real server.
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
from fastapi.testclient import TestClient

import auth
import bodylimit
import main
import requestid
from runs import RunState, RunStore

TOKEN = "a-shared-secret"
SENT = "hub-correlation-0001"


@pytest.fixture
def client(monkeypatch):
    """The real routes behind bearer authentication, so a 401 is reachable.

    A fresh app rather than main.app: middleware cannot be added to an
    application that has already started, and main.app is shared with every
    other test module in the session. The routes themselves are the real
    objects, so the handlers still close over main.RUNS and can be redirected.
    """
    from fastapi import FastAPI

    provider = auth.BearerAuth(TOKEN)
    monkeypatch.setattr(main, "AUTH", provider)

    app = FastAPI()
    for route in main.app.routes:
        if str(getattr(route, "path", "")).startswith("/runs"):
            app.router.routes.append(route)

    # main.py's order, and the order matters: the body size limit is innermost,
    # authentication next, the request id outermost. A separate test pins that
    # main.py really is in this order, so this fixture keeps describing it.
    app.add_middleware(bodylimit.BodySizeLimitMiddleware)
    app.add_middleware(auth.AuthenticationMiddleware, provider=provider)
    app.add_middleware(requestid.RequestIdMiddleware)
    with TestClient(app) as c:
        yield c


def _full_store():
    """A store that refuses admission: every retained run still in flight."""
    store = RunStore(max_runs=2)
    for run in [store.create() for _ in range(2)]:
        store.advance(run.run_id, RunState.INITIALIZING)
        store.advance(run.run_id, RunState.RUNNING)
    return store


# --- the case the issue is about: a refusal a hub can place ----------------

def test_a_refusal_at_capacity_carries_the_id_the_hub_sent(client, monkeypatch):
    """The headline. There is no run_id to report, because no run was created,
    so this header is the only thing tying the 503 to what was submitted."""
    monkeypatch.setattr(main, "RUNS", _full_store())
    refused = client.post(
        "/runs",
        data={"biochef_workflow": "{}"},
        files=[("files", ("input-1-out", b"in", "application/octet-stream"))],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": SENT},
    )
    assert refused.status_code == 503
    assert refused.headers["x-request-id"] == SENT
    assert "run_id" not in refused.text, "still no run, which is the point"


def test_a_rejected_caller_is_told_which_request_was_rejected(client):
    """A 401 is written by the authentication layer, which never calls the
    application. An id on it means the middleware is genuinely outside it."""
    refused = client.post("/runs", headers={"X-Request-Id": SENT})
    assert refused.status_code == 401
    assert refused.headers["x-request-id"] == SENT


def test_an_oversized_body_is_refused_with_an_id(client):
    """413 comes from the body size limit, which answers without calling the
    application either."""
    refused = client.post("/runs", headers={
        "Authorization": f"Bearer {TOKEN}", "content-length": "999999999999"})
    assert refused.status_code == 413
    assert refused.headers["x-request-id"]


@pytest.mark.parametrize("call, expected", [
    (lambda c, h: c.post("/runs", headers=h), 422),
    (lambda c, h: c.get("/runs/nope", headers=h), 404),
    (lambda c, h: c.post("/runs/nope/cancel", headers=h), 404),
    (lambda c, h: c.get("/runs/nope/logs", headers=h), 404),
])
def test_every_other_outcome_carries_one_too(client, call, expected):
    response = call(client, {"Authorization": f"Bearer {TOKEN}",
                             "X-Request-Id": SENT})
    assert response.status_code == expected
    assert response.headers["x-request-id"] == SENT


# --- the run remembers which request asked for it ---------------------------

def test_an_accepted_run_carries_the_id_back(client):
    accepted = client.post(
        "/runs",
        data={"biochef_workflow": "not json"},
        files=[("files", ("input-1-out", b"in", "application/octet-stream"))],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": SENT},
    )
    assert accepted.status_code == 202
    assert accepted.headers["x-request-id"] == SENT
    assert accepted.json()["request_id"] == SENT, (
        "in the body as well as the header: a hub whose response was lost in "
        "transit never learned the run_id, and polling is how it recovers"
    )


def test_the_run_still_carries_it_when_polled_later(client):
    run_id = client.post(
        "/runs",
        data={"biochef_workflow": "not json"},
        files=[("files", ("input-1-out", b"in", "application/octet-stream"))],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": SENT},
    ).json()["run_id"]

    polled = client.get(f"/runs/{run_id}",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert polled.json()["request_id"] == SENT, (
        "the run's own id, not the id of the request doing the polling"
    )


def test_a_run_with_no_request_behind_it_omits_the_field():
    """Absent rather than null, matching how error and outputs behave."""
    run = RunStore().create()
    assert "request_id" not in run.as_dict()
    assert run.request_id is None


# --- what gets accepted, and what gets replaced -----------------------------

def test_an_absent_id_is_generated(client):
    response = client.get("/runs/nope",
                          headers={"Authorization": f"Bearer {TOKEN}"})
    generated = response.headers["x-request-id"]
    assert len(generated) == 32 and all(c in "0123456789abcdef" for c in generated)


def test_two_requests_without_an_id_do_not_share_one(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = client.get("/runs/nope", headers=headers).headers["x-request-id"]
    second = client.get("/runs/nope", headers=headers).headers["x-request-id"]
    assert first != second


@pytest.mark.parametrize("hostile", [
    "a\r\nX-Injected: yes",
    "a\nX-Injected: yes",
    "a\rb",
    "a\x00b",
    "a b",
    "a\tb",
    "\x7f",
    "a" * (requestid.MAX_LENGTH + 1),
    "",
])
def test_an_unusable_id_is_replaced_rather_than_echoed(client, hostile):
    """Replaced, not refused: rejecting a workflow submission over a header
    nobody needs would be a worse failure than ignoring the header."""
    response = client.get("/runs/nope", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Request-Id": hostile})
    assert response.status_code == 404, "the request itself still goes through"
    emitted = response.headers["x-request-id"]
    assert emitted != hostile
    assert len(emitted) == 32


@pytest.mark.parametrize("unsendable", [
    "ol\u00e1", "\u65e5\u672c", "caf\u00e9", "\u200b", "a\u0085b",
])
def test_non_ascii_is_refused_at_the_seam_rather_than_over_http(unsendable):
    """Tested against accept() directly, because httpx will not send it.

    A header must be ASCII to leave the test client, so the parametrized HTTP
    cases above cannot reach this -- the one that tried errored inside httpx
    and never exercised the service at all. Over a real wire it can arrive: a
    server decoding received header bytes as latin-1 hands up a string like
    this. The guard lives in accept(), so that is where it is checked.
    """
    used, from_caller = requestid.accept(unsendable)
    assert from_caller is False
    assert used != unsendable
    assert used.isascii()


def test_the_limit_is_wide_enough_for_the_ids_a_hub_would_actually_send():
    """Stated as what it has to accommodate, not as the number.

    The boundary tests below are written in terms of MAX_LENGTH, so they move
    with it and cannot notice it changing -- a mutation lowering it to 127 left
    the whole file green. Shrink it to 8 and every hub using a prefixed UUID
    would silently lose correlation, because an id that is merely too long is
    replaced rather than refused: nothing fails, work still runs, and the
    correlation quietly stops working.

    The upper bound matters too: the value is echoed on every response and kept
    on a retained run record, so MAX_RUNS copies of it are held.
    """
    realistic = [
        "123e4567-e89b-12d3-a456-426614174000",      # canonical UUID, 36
        "biochef-hub/123e4567-e89b-12d3-a456-426614174000",
        "run-2026-10-04T00:00:00Z-0001",
        requestid.generate(),
    ]
    for candidate in realistic:
        assert requestid.accept(candidate)[1] is True, (
            f"{candidate!r} ({len(candidate)} chars) is the shape of id a hub "
            f"sends, and MAX_LENGTH={requestid.MAX_LENGTH} refuses it"
        )
    assert requestid.MAX_LENGTH <= 256, (
        "echoed on every response and kept on every retained run record"
    )


def test_the_longest_acceptable_id_is_still_accepted(client):
    """The boundary, from the usable side, so the limit is not off by one."""
    longest = "a" * requestid.MAX_LENGTH
    response = client.get("/runs/nope", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Request-Id": longest})
    assert response.headers["x-request-id"] == longest


def test_a_caller_can_tell_its_value_was_not_taken(client):
    """The only signal, and it is sufficient: compare what came back."""
    sent = "a" * (requestid.MAX_LENGTH + 1)
    got = client.get("/runs/nope", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Request-Id": sent}
    ).headers["x-request-id"]
    assert got != sent


# --- the property that matters, checked against the real arbiter ------------

def test_everything_this_service_can_emit_is_a_legal_header_value():
    """Checked against h11, not against the pattern used to accept it.

    h11 is what uvicorn speaks, and so what `fastapi run` serves. It raises
    LocalProtocolError rather than writing an illegal header, which would turn
    an otherwise-fine response into a dead one. Asserting the emitted id
    matches requestid.ACCEPTABLE would be circular -- the pattern and the
    implementation are the same decision.

    Both halves of the output are covered: ids generated here, and ids taken
    from a caller, including every character the pattern permits.
    """
    import h11

    permitted = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                 "0123456789._~:/+=@-")
    candidates = [requestid.generate() for _ in range(200)]
    candidates += [requestid.accept(permitted)[0],
                   requestid.accept(permitted * 2)[0],
                   requestid.accept("a" * requestid.MAX_LENGTH)[0]]
    candidates += [requestid.accept(c)[0] for c in permitted]
    candidates += [requestid.accept(h)[0] for h in (
        "a\r\nX: y", "a\nb", "a\x00b", "a b", "ol\u00e1", "\x7f", None, "",
        "a" * 5000)]

    for candidate in candidates:
        conn = h11.Connection(our_role=h11.SERVER)
        conn.receive_data(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        conn.next_event()
        conn.next_event()
        try:
            conn.send(h11.Response(
                status_code=200,
                headers=[("x-request-id", candidate), ("content-length", "0")]))
        except h11.LocalProtocolError as refused:
            raise AssertionError(
                f"this service would emit {candidate!r}, which a real server "
                f"refuses to write: {refused}") from None


def test_the_instrument_can_actually_fail():
    """Because the loop above passing means nothing if h11 accepts anything.

    An earlier version of a test in this file reported a path as handled when
    its own input never reached that path.
    """
    import h11

    conn = h11.Connection(our_role=h11.SERVER)
    conn.receive_data(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    conn.next_event()
    conn.next_event()
    with pytest.raises(h11.LocalProtocolError):
        conn.send(h11.Response(status_code=200,
                               headers=[("x-request-id", "a\r\nX: y"),
                                        ("content-length", "0")]))


# --- the ambiguous cases ----------------------------------------------------

def test_two_inbound_ids_resolve_to_the_first(client):
    """A proxy adding its own alongside the caller's. The first wins, which is
    what every header getter already does and what a caller is most likely
    matching on. Recorded so it is a decision rather than an accident."""
    response = client.get("/runs/nope", headers=[
        ("Authorization", f"Bearer {TOKEN}"),
        ("X-Request-Id", "first-one"),
        ("X-Request-Id", "second-one"),
    ])
    assert response.headers["x-request-id"] == "first-one"


def test_only_one_id_leaves_even_if_something_downstream_set_one():
    """Assigned, not appended. Two values on the way out would be exactly the
    ambiguity this refuses to accept on the way in."""
    from fastapi import FastAPI
    from starlette.responses import JSONResponse

    app = FastAPI()

    @app.get("/x")
    async def x():
        return JSONResponse({}, headers={"X-Request-Id": "set-by-the-handler"})

    app.add_middleware(requestid.RequestIdMiddleware)
    response = TestClient(app).get("/x", headers={"X-Request-Id": "from-caller"})
    assert response.headers.get_list("x-request-id") == ["from-caller"]


def test_an_unhandled_exception_comes_back_without_one():
    """A known boundary, recorded rather than discovered later.

    starlette's ServerErrorMiddleware sits outside every middleware the
    application adds, so the 500 it writes never passes back through here.
    Tagging it would mean owning the exception handler, which is a different
    change. If this ever starts failing, the limitation has been fixed and the
    docstring in requestid.py should stop claiming it.
    """
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("unhandled")

    app.add_middleware(requestid.RequestIdMiddleware)
    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.headers.get("x-request-id") is None


# --- where it sits in the stack ---------------------------------------------

def test_it_is_the_outermost_layer():
    """Added last, or the refusals it exists to label leave untagged.

    Pinned on comment-stripped source, because grep matches a comment as
    readily as code -- and the lines above this one are mostly comment.
    """
    import re

    code = re.sub(r"#.*", "", (REPO_ROOT / "main.py").read_text())
    positions = {
        name: code.index(f"add_middleware({name}")
        for name in ("BodySizeLimitMiddleware", "AuthenticationMiddleware",
                     "RequestIdMiddleware")
    }
    assert positions["RequestIdMiddleware"] == max(positions.values()), (
        f"RequestIdMiddleware must be added last; order is "
        f"{sorted(positions, key=positions.get)}"
    )


def test_a_handler_reads_the_validated_value_and_not_the_raw_header():
    """Because the raw header is the untrusted input this exists to contain.

    If submit_run read request.headers directly, a hostile value would reach
    the run record and the response body however careful the middleware was.
    """
    source = (REPO_ROOT / "main.py").read_text()
    assert 'request.state, "request_id"' in source
    assert 'headers.get("x-request-id")' not in source.lower()
    assert 'headers["x-request-id"]' not in source.lower()
