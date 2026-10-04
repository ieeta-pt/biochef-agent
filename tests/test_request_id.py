"""A hub cannot correlate what it sent with what this agent did (#84).

Recorded before the fix, as the roadmap asks. Every assertion here states
something that is TRUE of the current service and should stop being true, so
each one is expected to be deleted or inverted by the implementing commit.

The gap is worst on the refusals. POST /runs answers 503 when run capacity is
reached and 401 when credentials are wrong, and both carry nothing but a detail
string -- no run_id, because no run was created. A hub that submitted ten
workflows and got three refusals cannot say which three without matching on the
bodies it sent. Same for a 413 from the body limit and a 422 from a malformed
workflow.

On success it is only slightly better: the run_id is one the agent invented, so
if the response is lost in transit the run is orphaned -- executing here,
unknown there.

Two of these tests record facts about the INSTRUMENTS rather than the service,
because they decide what the implementing commit's tests are allowed to claim.
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
import main
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
    app.add_middleware(auth.AuthenticationMiddleware, provider=provider)
    with TestClient(app) as c:
        yield c


def _full_store():
    """A store that refuses admission: every retained run still in flight."""
    store = RunStore(max_runs=2)
    for run in [store.create() for _ in range(2)]:
        store.advance(run.run_id, RunState.INITIALIZING)
        store.advance(run.run_id, RunState.RUNNING)
    return store


# --- nothing carries an id --------------------------------------------------

def test_no_request_id_middleware_exists():
    """Searched by behaviour as well as by name, so a differently-named one
    still counts: nothing in the tree reads or writes a correlation header."""
    sources = {p.name: p.read_text() for p in REPO_ROOT.glob("*.py")}
    joined = "\n".join(sources.values()).lower()
    for spelling in ("x-request-id", "x-correlation-id", "request_id",
                     "correlation_id", "traceparent"):
        assert spelling not in joined, f"{spelling!r} appears already"


def test_a_refused_submission_gives_the_hub_no_handle(client, monkeypatch):
    """503 with a detail string and a Retry-After, and nothing else.

    This is the case that matters most: there is no run_id to report, so the
    hub has nothing at all to tie the refusal back to what it sent.
    """
    monkeypatch.setattr(main, "RUNS", _full_store())
    refused = client.post(
        "/runs",
        data={"biochef_workflow": "{}"},
        files=[("files", ("input-1-out", b"in", "application/octet-stream"))],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": SENT},
    )
    assert refused.status_code == 503
    assert set(refused.json()) == {"detail"}
    assert "run_id" not in refused.text
    assert refused.headers.get("x-request-id") is None, (
        "the value the hub sent is not echoed"
    )
    assert SENT not in refused.text


def test_a_rejected_caller_gets_no_handle_either(client):
    refused = client.post("/runs", headers={"X-Request-Id": SENT})
    assert refused.status_code == 401
    assert refused.headers.get("x-request-id") is None
    assert SENT not in refused.text


def test_an_accepted_submission_echoes_nothing_the_hub_chose(client):
    """The run_id comes back, but it is the agent's own invention."""
    accepted = client.post(
        "/runs",
        data={"biochef_workflow": "not json"},
        files=[("files", ("input-1-out", b"in", "application/octet-stream"))],
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": SENT},
    )
    assert accepted.status_code == 202, (
        "pinned so this cannot quietly start measuring a refusal instead"
    )
    assert set(accepted.json()) == {"run_id", "state"}
    assert accepted.headers.get("x-request-id") is None
    assert SENT not in accepted.text


def test_a_run_record_cannot_say_which_request_asked_for_it():
    store = RunStore()
    run = store.create()
    assert "request_id" not in run.as_dict()
    assert not hasattr(run, "request_id")


# --- what the instruments can and cannot show -------------------------------

def test_the_test_client_cannot_demonstrate_header_injection():
    """Recorded because it decides what the implementing commit may claim.

    TestClient does not go over the wire, so a CRLF inside a header value is
    carried through as one opaque value and no second header appears. A test
    that echoed a hostile id and then asserted no injected header was present
    would therefore pass against an implementation that validates nothing.

    The implementing commit must assert the property on what it EMITS, checked
    against something that actually parses headers -- see the next test.
    """
    from fastapi import FastAPI
    from starlette.responses import JSONResponse

    app = FastAPI()

    @app.get("/x")
    async def x():
        return JSONResponse({}, headers={"X-Request-Id": "a\r\nX-Injected: y"})

    response = TestClient(app).get("/x")
    assert response.headers.get("x-injected") is None, (
        "if this ever fails, TestClient has started splitting headers and the "
        "weaker test shape would become valid"
    )
    assert response.headers["x-request-id"] == "a\r\nX-Injected: y", (
        "carried through untouched, which is why it proves nothing"
    )


def test_the_real_wire_protocol_refuses_an_illegal_header_value():
    """So the failure to guard against is not injection but a dead response.

    h11 -- what uvicorn speaks, and so what `fastapi run` serves -- raises
    rather than writing the header. Echoing an unvalidated caller header
    therefore turns a cosmetic header into the destruction of that caller's own
    otherwise-fine response, which is a worse outcome than ignoring the header.

    This is the arbiter the implementing commit should check its emitted ids
    against, rather than against a charset of my own choosing.
    """
    import h11

    conn = h11.Connection(our_role=h11.SERVER)
    conn.receive_data(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    conn.next_event()
    conn.next_event()

    with pytest.raises(h11.LocalProtocolError):
        conn.send(h11.Response(
            status_code=200,
            headers=[("x-request-id", "a\r\nX-Injected: y"),
                     ("content-length", "0")]))


def test_nothing_bounds_how_long_an_inbound_header_may_be(client):
    """A value this service would store on a retained run record.

    The server caps total header size, but this application caps nothing, and
    the cap is the server's to change.
    """
    enormous = "a" * 100_000
    response = client.get("/runs/nope",
                          headers={"Authorization": f"Bearer {TOKEN}",
                                   "X-Request-Id": enormous})
    assert response.status_code == 404
    assert response.headers.get("x-request-id") is None
