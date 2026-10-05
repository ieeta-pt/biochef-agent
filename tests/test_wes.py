"""The GA4GH WES surface (#24, F5).

B1 took the WES RunState vocabulary and put cancel at WES's path so that this
would be a mapping rather than a rewrite. What follows checks that it is one --
that a run is the same run through either API -- and checks the place a WES
server is most tempted to lie, which is service-info.

Advertising CWL would make this server discoverable by every conformant client,
and every one of them would fail after uploading its data. Being undiscoverable
is better than that, and service-info exists precisely so a client can find out
before it commits.
"""

import json
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

import main
import wes
from runs import RunState, RunStore


@pytest.fixture
def client():
    return TestClient(main.app)


# --- service-info ------------------------------------------------------------

def test_service_info_declares_only_what_this_server_runs(client):
    body = client.get(f"{wes.BASE}/service-info").json()
    assert list(body["workflow_type_versions"]) == ["BIOCHEF"]
    for lie in ("CWL", "WDL", "NFL"):
        assert lie not in body["workflow_type_versions"], (
            f"service-info advertises {lie}, which this server cannot run; every "
            f"conformant client would discover it and fail after uploading"
        )


def test_service_info_carries_the_wes_version_and_shape(client):
    body = client.get(f"{wes.BASE}/service-info").json()
    assert body["type"] == {"group": "org.ga4gh", "artifact": "wes",
                            "version": wes.WES_VERSION}
    assert wes.WES_VERSION in body["supported_wes_versions"]
    for required in ("id", "name", "system_state_counts",
                     "supported_filesystem_protocols", "workflow_engine_versions"):
        assert required in body, f"service-info is missing {required}"


def test_service_info_says_where_the_standard_surface_stops(client):
    """A client depending on pagination should find that out from the server."""
    body = client.get(f"{wes.BASE}/service-info").json()
    not_implemented = body["tags"]["not_implemented"]
    assert "page" in not_implemented
    assert "remote workflow URLs" in not_implemented


def test_service_info_counts_come_from_the_live_store():
    store = RunStore()
    a = store.create()
    b = store.create()
    store.advance(a.run_id, RunState.INITIALIZING)
    counts = store.state_counts()
    assert counts["INITIALIZING"] == 1
    assert counts["QUEUED"] == 1


# --- refusing what it cannot run --------------------------------------------

def test_a_cwl_submission_is_refused_by_name(client):
    response = client.post(
        f"{wes.BASE}/runs",
        data={"workflow_type": "CWL", "workflow_url": "main.cwl"},
        files={"workflow_attachment": ("main.cwl", b"cwlVersion: v1.2")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "BIOCHEF" in detail, "the refusal must say what WOULD be accepted"
    assert "service-info" in detail


def test_a_wrong_version_of_the_right_type_is_refused(client):
    response = client.post(
        f"{wes.BASE}/runs",
        data={"workflow_type": "BIOCHEF", "workflow_type_version": "99",
              "workflow_url": "w.json"},
        files={"workflow_attachment": ("w.json", b"{}")},
    )
    assert response.status_code == 400


def test_a_remote_workflow_url_is_refused():
    """Fetching one would make this service execute a document from wherever a
    caller pointed it, which is the problem E1 exists to solve."""
    with pytest.raises(wes.MissingWorkflow) as caught:
        wes.select_workflow("https://elsewhere.test/w.json", [("w.json", "/tmp/x")])
    assert "remote" in str(caught.value)


def test_a_workflow_url_naming_no_attachment_is_refused_with_the_names():
    with pytest.raises(wes.MissingWorkflow) as caught:
        wes.select_workflow("absent.json", [("a.fa", "/tmp/a"), ("b.fa", "/tmp/b")])
    message = str(caught.value)
    assert "a.fa" in message and "b.fa" in message, (
        "the refusal should say what WAS attached"
    )


def test_a_missing_workflow_url_is_refused():
    with pytest.raises(wes.MissingWorkflow):
        wes.select_workflow("", [("a.fa", "/tmp/a")])


# --- the mapping -------------------------------------------------------------

def test_the_workflow_attachment_is_separated_from_the_inputs():
    workflow, inputs = wes.select_workflow(
        "flow.json",
        [("reads.fq", "/tmp/r"), ("flow.json", "/tmp/f"), ("ref.fa", "/tmp/g")],
    )
    assert workflow == ("flow.json", "/tmp/f")
    assert [name for name, _ in inputs] == ["reads.fq", "ref.fa"]


def test_a_leading_dot_slash_in_the_workflow_url_still_matches():
    """WES describes workflow_url as relative to the attachments."""
    workflow, inputs = wes.select_workflow(
        "./flow.json", [("flow.json", "/tmp/f"), ("in.fa", "/tmp/i")])
    assert workflow[0] == "flow.json"
    assert len(inputs) == 1


def test_run_status_uses_the_wes_vocabulary():
    store = RunStore()
    run = store.create()
    store.advance(run.run_id, RunState.INITIALIZING)
    body = wes.run_status(store.get(run.run_id))
    assert body == {"run_id": run.run_id, "state": "INITIALIZING"}


def test_run_log_does_not_invent_fields_it_does_not_know():
    """A fabricated exit code is worse than an absent one: a client cannot tell
    a real zero from a placeholder."""
    store = RunStore()
    run = store.create()
    body = wes.run_log(store.get(run.run_id))
    assert body["outputs"] == {}
    assert body["task_logs"] == []
    assert "exit_code" not in body["run_log"]


def test_a_failure_reason_reaches_somewhere_a_client_looks():
    store = RunStore()
    run = store.create()
    store.advance(run.run_id, RunState.INITIALIZING)
    store.advance(run.run_id, RunState.SYSTEM_ERROR, error="the disk went away")
    body = wes.run_log(store.get(run.run_id))
    assert body["state"] == "SYSTEM_ERROR"
    assert "the disk went away" in body["run_log"]["stderr"]


def test_the_run_list_terminates_for_a_paging_client():
    """next_page_token is typed as a string. A client paging until it is empty
    must terminate rather than trip over a missing key."""
    store = RunStore()
    store.create()
    body = wes.run_list(store.all())
    assert body["next_page_token"] == ""
    assert len(body["runs"]) == 1


def test_listing_does_not_hand_out_the_live_collection():
    """Iterating the store's own dict while a worker advances a run is a
    RuntimeError in the reader and a puzzle to diagnose in a request that merely
    listed something."""
    store = RunStore()
    store.create()
    listed = store.all()
    store.create()
    assert len(listed) == 1, "the caller's list changed underneath it"


# --- the endpoints answer ----------------------------------------------------

def test_an_unknown_run_is_a_404_through_both_wes_endpoints(client):
    assert client.get(f"{wes.BASE}/runs/nope/status").status_code == 404
    assert client.get(f"{wes.BASE}/runs/nope").status_code == 404


def test_the_run_list_endpoint_accepts_the_paging_parameters_it_ignores(client):
    """Refusing them would fail conformant clients that send a page size by
    default."""
    assert client.get(f"{wes.BASE}/runs", params={"page_size": 10}).status_code == 200


def test_both_apis_read_the_same_store(client):
    """The claim this whole layer rests on.

    If WES kept its own registry, a run submitted through one API would be
    invisible to the other, and the standard surface would be a second service
    wearing the same hostname.
    """
    run = main.RUNS.create()
    main.RUNS.advance(run.run_id, RunState.INITIALIZING)

    bespoke = client.get(f"/runs/{run.run_id}").json()
    standard = client.get(f"{wes.BASE}/runs/{run.run_id}/status").json()
    assert bespoke["run_id"] == standard["run_id"] == run.run_id
    assert bespoke["state"] == standard["state"] == "INITIALIZING"

    listed = client.get(f"{wes.BASE}/runs").json()["runs"]
    assert any(entry["run_id"] == run.run_id for entry in listed)


def test_the_bespoke_endpoints_are_still_there(client):
    """Removing them to prove a point about standards would break the one client
    this service has."""
    paths = {route.path for route in main.app.routes if hasattr(route, "path")}
    assert "/runs" in paths
    assert "/runs/{run_id}" in paths
    assert "/convert" in paths
