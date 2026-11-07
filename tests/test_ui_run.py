"""The Run screen (mock 05), in both egress modes."""

import re

import pytest

import audit
from store import db, repo, seed
from store.fixtures import mock
from store.states import EgressState, SubjectType
from tests.store_support import fresh_database
from tests.ui_support import KEYS, client_for, dev_settings


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    return bind


def web(engine, mode="airlock"):
    return client_for(dev_settings(node_name=mock.NODE, dev_user="j.silva",
                                   egress_mode=mode), bind=engine)


def post(client, path):
    page = client.get("/ui/inbox").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return client.post(path, headers={"X-CSRF-Token": token})


def to_review(engine, run_id="run_8e90", state="COMPLETE"):
    """A run of a request, waiting for a person, in a store seeded for airlock."""
    with db.writing(engine) as session:
        repo.save_run(session, run_id, state=state, egress_files=list(mock.OUTPUT_FILES))
        repo.set_egress_state(session, run_id, EgressState.RULES_PENDING)
        repo.set_egress_state(session, run_id, EgressState.AWAITING_REVIEW)


def test_the_header_of_the_mock(engine):
    text = web(engine).get("/ui/runs/run_8f21").text
    assert "Run of req_8f21" in text
    assert "requested by Rui Matos" in text
    assert "cardio-pt · CARDIO-PT cohort · v2" in text and "gwas-qc · 1.3.2" in text
    assert ">COMPLETE<" in text and "Waiting for the airlock" in text


def test_the_steps_strip(engine):
    text = web(engine).get("/ui/runs/run_8f21").text
    for name in mock.STEP_MINUTES:
        assert name in text
    assert "COMPLETE · 47 min" in text


def test_the_airlock_stages(engine):
    text = web(engine).get("/ui/runs/run_8f21").text
    assert "2 · TRE airlock" in text and "Handed to the airlock" in text
    assert "Waiting for the TRE&#39;s decision" in text
    assert "2 files pass, 1 held back" in text


def test_the_file_table_with_held_back_rows_in_warn_colours(engine):
    text = web(engine).get("/ui/runs/run_8f21").text
    assert "assoc_summary.tsv" in text and "1.8 MB" in text
    assert "held back · type not allowed by the study" in text
    assert '<tr class="bg-warn-soft">' in text


def test_airlock_mode_has_no_review_buttons(engine):
    text = web(engine).get("/ui/runs/run_8f21").text
    assert "Release" not in text and "Withhold" not in text


def test_manual_mode_offers_the_review(engine):
    to_review(engine)
    text = web(engine, "manual").get("/ui/runs/run_8e90").text
    assert "Release 2 files and sign" in text and "Withhold and sign" in text
    assert "2 · Review in the agent" in text and "Waiting for your review" in text


def test_a_release_from_the_page(engine, tmp_path):
    to_review(engine)
    client = web(engine, "manual")
    response = post(client, "/ui/_/runs/run_8e90/release")
    assert response.headers["hx-redirect"] == "/ui/runs/run_8e90"
    text = client.get("/ui/runs/run_8e90").text
    assert "Released by j.silva" in text and "Undo before the next poll" in text
    assert audit.read(str(tmp_path / "audit.jsonl"))[-1]["action"] == "release"


def test_a_withholding_from_the_page(engine):
    to_review(engine)
    client = web(engine, "manual")
    post(client, "/ui/_/runs/run_8e90/withhold")
    text = client.get("/ui/runs/run_8e90").text
    assert "The requester gets no result from this node." in text
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_8e90").retain_until is not None


def test_undo_of_a_release_from_the_page(engine):
    to_review(engine)
    client = web(engine, "manual")
    post(client, "/ui/_/runs/run_8e90/release")
    path = re.search(r"/ui/_/decisions/\d+/undo", client.get("/ui/runs/run_8e90").text).group(0)
    assert post(client, path).headers["hx-refresh"] == "true"
    assert "Release 2 files and sign" in client.get("/ui/runs/run_8e90").text


def test_airlock_mode_refuses_a_release_past_the_page(engine):
    response = post(web(engine), "/ui/_/runs/run_8f21/release")
    assert response.status_code == 409 and "airlock" in response.text
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_8f21").egress_state == "awaiting_airlock"


def test_cancel_is_there_only_while_it_means_something(engine):
    assert ">Cancel run</button>" in web(engine).get("/ui/runs/run_8e90").text
    assert ">Cancel run</button>" not in web(engine).get("/ui/runs/run_8f21").text


def test_cancel_sets_the_flag_and_records_who(engine):
    client = web(engine)
    post(client, "/ui/_/runs/run_8e90/cancel")
    with db.reading(engine) as session:
        assert repo.get_run(session, "run_8e90").cancel_requested is True
        row = repo.decisions_about(session, SubjectType.RUN, "run_8e90")[0]
    assert (row.action, row.user) == ("cancel", "j.silva")
    assert "Cancelling" in client.get("/ui/runs/run_8e90").text


def test_a_finished_run_cannot_be_cancelled(engine):
    assert post(web(engine), "/ui/_/runs/run_8f21/cancel").status_code == 409


def test_the_log_polls_only_while_the_run_is_active(engine):
    client = web(engine)
    live = client.get("/ui/_/runs/run_8e90/tab/logs").text
    assert 'hx-trigger="every 2s"' in live and "data-live" in live
    done = client.get("/ui/_/runs/run_8f21/tab/logs").text
    assert "every 2s" not in done


def test_a_log_is_text(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_8f21", state="COMPLETE",
                      log_stdout="<script>alert(1)</script>")
    text = web(engine).get("/ui/_/runs/run_8f21/tab/logs").text
    assert "<script>alert" not in text and "&lt;script&gt;" in text


def test_a_step_log_waits_for_the_run_to_end(engine):
    text = web(engine).get("/ui/_/runs/run_8e90/tab/logs?log=summarise").text
    assert "Per step logs exist only after the run ends." in text


def test_a_step_log_is_shown(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_8f21", state="COMPLETE",
                      node_logs={"assoc": {"stdout": "3 tests", "stderr": ""}})
    assert "3 tests" in web(engine).get("/ui/_/runs/run_8f21/tab/logs?log=assoc").text


def test_logs_download_as_a_file(engine):
    response = web(engine).get("/ui/runs/run_8f21/download/log")
    assert "attachment" in response.headers["content-disposition"]
    assert "fixture log" in response.text


def test_the_manifest_while_the_workspace_is_kept(engine, tmp_path):
    path = tmp_path / "run.json"
    path.write_text('{"runner": "apptainer"}')
    with db.writing(engine) as session:
        repo.save_run(session, "run_8f21", state="COMPLETE", manifest_path=str(path))
    client = web(engine)
    assert "apptainer" in client.get("/ui/_/runs/run_8f21/tab/manifest").text
    response = client.get("/ui/runs/run_8f21/download/run.json")
    assert response.json() == {"runner": "apptainer"}


def test_a_manifest_that_is_gone_says_so(engine):
    client = web(engine)
    assert "run.json is not available" in client.get("/ui/_/runs/run_8f21/tab/manifest").text
    assert client.get("/ui/runs/run_8f21/download/run.json").status_code == 404


def test_a_link_cannot_stand_in_for_the_manifest(engine, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("secret")
    link = tmp_path / "run.json"
    link.symlink_to(secret)
    with db.writing(engine) as session:
        repo.save_run(session, "run_8f21", state="COMPLETE", manifest_path=str(link))
    assert "secret" not in web(engine).get("/ui/_/runs/run_8f21/tab/manifest").text


def test_an_unknown_run_or_tab_is_a_404(engine):
    client = web(engine)
    assert client.get("/ui/runs/nobody").status_code == 404
    assert client.get("/ui/_/runs/run_8f21/tab/nonsense").status_code == 404


def test_requests_link_to_the_run(engine):
    client = web(engine)
    text = client.get("/ui/requests?selected=req_8f21").text
    assert 'href="/ui/runs/run_8f21"' in text and "Open the run" in text
    assert 'href="/ui/runs/run_8f21"' in client.get(
        f"/ui/_/studies/{mock.CARDIO}/tab/requests").text


def test_a_run_from_the_plain_api_has_no_request(engine):
    with db.writing(engine) as session:
        repo.save_run(session, "run_api", state="COMPLETE")
    text = web(engine).get("/ui/runs/run_api").text
    assert "Not from a study request" in text
