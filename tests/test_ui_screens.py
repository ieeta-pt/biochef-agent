"""Screens 01 to 03 with the seeded data, in every state of the mocks (part 4)."""

import json
import re
from datetime import timedelta

import pytest

import audit
from node import decisions
from node.policy import FakePolicyEngine
from store import db, repo, seed
from store.fixtures import mock
from store.models import Decision, Request as RequestRow
from store.states import SubjectType, Verdict
from sqlmodel import select
from tests.store_support import fresh_database
from tests.ui_support import KEYS, client_for, dev_settings

ALERT_ID = "c2e86f10…@lifescience-ri.eu"


def settings(**overrides):
    fields = {"node_name": mock.NODE, "required_visa": "ControlledAccessGrants",
              "dev_user": "j.silva"}
    return dev_settings(**{**fields, **overrides})


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    return bind


@pytest.fixture
def trail(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def client(engine):
    return client_for(settings(), bind=engine, policy=FakePolicyEngine())


def post(client, path, **data):
    page = client.get("/ui/inbox").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return client.post(path, data=data, headers={"X-CSRF-Token": token,
                                                 "HX-Request": "true"})


def version_id(engine, study, number):
    with db.reading(engine) as session:
        return repo.study_version_named(session, study, number).id


def workflow_id(engine, name, version):
    with db.reading(engine) as session:
        rows = [row for row in repo.pending_workflow_versions(session)
                if (row.name, row.version) == (name, version)]
        return rows[0].id


def state_of(engine, study, number):
    with db.reading(engine) as session:
        return repo.study_version_named(session, study, number).state


def cards(text):
    return re.findall(r'class="text-base font-medium">([^<]+)<', text)


# Inbox (01)


def test_the_inbox_lists_the_three_waiting_versions_newest_first(client):
    assert cards(client.get("/ui/inbox").text) == [
        "cardio-pt · CARDIO-PT cohort · v3",
        "rare-variants-pilot · Rare variants pilot · v1", "gwas-qc · 1.4.0"]


def test_the_sidebar_counts_come_from_the_store(client):
    text = client.get("/ui/inbox").text
    assert re.search(r"Inbox\s*<span[^>]*>3<", text)
    assert re.search(r"Requests\s*<span[^>]*>2 refused<", text)
    assert "Coordinator polled" in text


def test_each_pill_of_the_mock(client):
    text = client.get("/ui/inbox").text
    for pill in ("Re-approval needed", "Check failed · cannot be approved",
                 "New workflow version"):
        assert pill in text


def test_a_new_study_is_marked_new_and_summarised(client, engine):
    text = client.get("/ui/inbox").text
    assert "New study. 2 members, 2 nodes." in text
    assert "Replaces approved v2. 1 member added, 1 output rule changed." in text
    assert "1 tool changed." in text


def test_a_new_study_without_trouble_says_new(engine, tmp_path):
    with db.writing(engine) as session:
        repo.add_study_version(
            session, study_id="fresh", version=1, digest="sha256:f", raw=b"{}",
            goal="Fresh", proposer="m-09", members=[{"member": "m-09", "ls_aai_id": "x"}],
            nodes=[], datasets=[], output_rules={})
        row = repo.study_version_named(session, "fresh", 1)
        repo.add_member_key(session, study_version_id=row.id, member="m-09",
                            ls_aai_id="x", signing_key={}, result_key="age1",
                            binding_token="t")
        session.flush()
        repo.mark_identity_verified(session, row.id, "m-09", {"sub": "x"})
    text = client_for(settings(), bind=engine).get("/ui/inbox").text
    assert '>New</span>' in text


def test_the_first_study_opens_by_default(client):
    text = client.get("/ui/inbox").text
    assert "<h2" in text and "Compared with" in text
    assert 'aria-current="true"' in text


def test_the_empty_state_is_the_one_in_the_spec(engine):
    with db.writing(engine) as session:
        for row in repo.pending_study_versions(session):
            repo.refuse_study_version(session, row.id)
        for row in repo.pending_workflow_versions(session):
            repo.refuse_workflow_version(session, row.id)
    text = client_for(settings(), bind=engine).get("/ui/inbox").text
    assert "Nothing is waiting for this node." in text


def test_a_card_loads_its_detail_in_place(client, engine):
    rare = version_id(engine, mock.RARE, 1)
    text = client.get("/ui/inbox").text
    assert f'hx-get="/ui/_/inbox/study/{rare}"' in text
    assert f'hx-push-url="/ui/inbox/study/{rare}"' in text
    part = client.get(f"/ui/_/inbox/study/{rare}").text
    assert "<html" not in part and "Rare variants pilot" in part
    assert 'hx-swap-oob="true"' in part


def test_the_detail_of_a_new_version_against_the_approved_one(client):
    text = client.get("/ui/inbox").text
    assert "Compared with" in text and "v2, approved" in text
    assert "Sofia Lima · new" in text
    assert "− min_count: 5" not in text
    assert "− min_count: 10" in text and "+ min_count: 5" in text
    assert "Output rules · changed" in text
    assert "Unchanged from v2" in text
    assert "EGAD00001009912" in text and "✓ bound to local data" in text
    assert "Visa required · ControlledAccessGrants" in text
    assert f"{mock.NODE} (this node)" in text
    assert "✓ all on the signed site list v14" in text
    assert "Submitter only" in text
    assert "Approving v3 supersedes v2." in text
    assert "withdraws" not in text


def test_names_come_with_their_role_and_never_as_an_opaque_id(client):
    text = client.get("/ui/inbox").text
    assert "Ana Costa · proposer" in text
    assert not re.search(r"\bm-0\d\b", text)
    assert "Proposed by Ana Costa" in text


def test_no_signature_is_claimed_that_nothing_checked(client):
    """The study's signature is not verified before part 8, nor is one on workflows."""
    assert "Signed by" not in client.get("/ui/inbox").text


def test_show_keys_is_a_toggle_over_the_members(client):
    text = client.get("/ui/inbox").text
    assert 'class="keys-toggle sr-only"' in text
    assert "Show keys" in text and "Hide keys" in text
    assert "signing es256:" in text and "result age1fixturem" in text


def test_the_fake_policy_engine_labels_itself(client):
    assert "Fake Policy Engine · weaker than this node&#39;s local rules." in (
        client.get("/ui/inbox").text)


def test_the_agents_own_check_says_what_it_leaves_open(engine):
    from node.policy import LocalRules
    text = client_for(settings(), bind=engine, policy=LocalRules()).get("/ui/inbox").text
    assert ("The agent&#39;s own check · types and sizes are checked here; min_count "
            "and aggregates_only cannot be until the Policy Engine is connected.") in text


def test_without_a_policy_engine_the_page_says_so(engine):
    text = client_for(settings(), bind=engine).get("/ui/inbox").text
    assert "Policy Engine not connected yet." in text
    assert "weaker" not in text


def test_the_policy_link_goes_where_it_is_configured(engine):
    url = "https://policy.tre.test/"
    text = client_for(settings(policy_engine_url=url), bind=engine).get("/ui/inbox").text
    assert f'<a href="{url}" target="_blank" rel="noopener noreferrer">See why' in text


def test_no_visa_setting_says_none_is_required(engine):
    text = client_for(settings(required_visa=None), bind=engine).get("/ui/inbox").text
    assert "No visa required by this node" in text


# A failed identity


@pytest.fixture
def rare(client, engine):
    return version_id(engine, mock.RARE, 1)


def test_approve_is_disabled_and_refuse_is_not(client, rare):
    text = client.get(f"/ui/inbox/study/{rare}").text
    buttons = re.findall(r"<button[^>]*>[^<]*sign</button>", text, re.S)
    approve = next(row for row in buttons if "Approve" in row)
    refuse = next(row for row in buttons if "Refuse" in row)
    assert re.search(r"\bdisabled\b", approve) and "hx-post" not in approve
    assert "disabled" not in refuse and "hx-post" in refuse


def test_the_alert_names_the_member_by_the_shortened_id(client, rare):
    text = client.get(f"/ui/inbox/study/{rare}").text
    assert 'role="alert"' in text and "This version cannot be approved" in text
    assert ALERT_ID in text
    assert "Paulo Neves" not in text
    assert "The keys listed are not the ones this LS Login sign in vouches for" in text
    assert "✗ identity not verified" in text
    assert "Marta Reis · proposer" in text


def test_a_failed_version_has_no_dac_box(client, rare):
    assert "data access committee" not in client.get(f"/ui/inbox/study/{rare}").text


def test_an_unbound_dataset_offers_to_bind_it(client, rare):
    text = client.get(f"/ui/inbox/study/{rare}").text
    assert "Not bound to local data yet" in text and "Bind it" in text


def test_posting_past_the_disabled_button_is_refused(client, engine, rare):
    response = post(client, f"/ui/_/inbox/study/{rare}/approve", dac_confirmed="on")
    assert response.status_code == 409
    assert 'role="alert"' in response.text
    assert state_of(engine, mock.RARE, 1) == "pending"


def test_a_failed_version_can_be_refused(client, engine, rare):
    response = post(client, f"/ui/_/inbox/study/{rare}/refuse")
    assert response.headers["hx-redirect"] == f"/ui/inbox/study/{rare}"
    assert state_of(engine, mock.RARE, 1) == "refused"


# Approve, refuse, undo


@pytest.fixture
def v3(engine):
    return version_id(engine, mock.CARDIO, 3)


def test_approve_needs_the_dac_tick(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/approve")
    assert response.status_code == 409
    assert "data access committee" in response.text
    assert state_of(engine, mock.CARDIO, 3) == "pending"


def test_approving_shows_the_status_line_of_the_spec(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/approve", dac_confirmed="on",
                    comment="fine")
    assert response.status_code == 200
    text = client.get(response.headers["hx-redirect"]).text
    assert ("v3 approved and signed. v2 was superseded at the same time; its runs "
            "and logs stay in the study history.") in text
    assert "Undo before the next poll" in text
    assert state_of(engine, mock.CARDIO, 2) == "superseded"


def test_after_a_decision_the_inbox_moves_on(client, engine, v3):
    post(client, f"/ui/_/inbox/study/{v3}/approve", dac_confirmed="on")
    text = client.get("/ui/inbox").text
    assert cards(text) == ["rare-variants-pilot · Rare variants pilot · v1"]
    assert re.search(r"Inbox\s*<span[^>]*>1<", text)


def test_the_decision_records_the_user_and_the_tick(client, engine, v3, trail):
    post(client, f"/ui/_/inbox/study/{v3}/approve", dac_confirmed="on", comment="fine")
    with db.reading(engine) as session:
        row = repo.decisions_about(session, SubjectType.STUDY_VERSION, v3)[0]
    signed = decisions.verify(row.object_raw, row.signature, KEYS.signing.public_key())
    assert (signed["user"], signed["dac_confirmed"], signed["comment"]) == (
        "j.silva", True, "fine")
    assert signed["node"] == mock.NODE
    assert [event["user"] for event in audit.read(str(trail))] == ["j.silva"] * 2


def test_refusing_keeps_the_comment_and_v2(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse", comment="too low")
    text = client.get(response.headers["hx-redirect"]).text
    assert "v3 refused and signed, with your comment. v2 stays approved." in text
    assert state_of(engine, mock.CARDIO, 2) == "approved"


def undo_of(page):
    return re.search(r'/ui/_/decisions/(\d+)/undo', page).group(0)


def test_undo_puts_everything_back_and_refreshes(client, engine, v3, trail):
    response = post(client, f"/ui/_/inbox/study/{v3}/approve", dac_confirmed="on")
    page = client.get(response.headers["hx-redirect"]).text
    undone = post(client, undo_of(page))
    assert undone.headers["hx-refresh"] == "true"
    assert state_of(engine, mock.CARDIO, 3) == "pending"
    assert state_of(engine, mock.CARDIO, 2) == "approved"
    assert len(cards(client.get("/ui/inbox").text)) == 3
    assert "node.decision.undone" in [row["event"] for row in audit.read(str(trail))]


def test_undo_is_gone_once_the_decision_is_posted(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse")
    url = response.headers["hx-redirect"]
    with db.writing(engine) as session:
        for row in repo.unposted_decisions(session):
            repo.mark_posted(session, row.id)
    assert "Undo before the next poll" not in client.get(url).text


def test_undo_is_gone_once_the_poll_has_claimed_the_decision(client, engine, v3):
    """Claimed and not yet answered: the POST may already have arrived."""
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse")
    url = response.headers["hx-redirect"]
    assert "v3 refused and signed" in client.get(url).text
    with db.writing(engine) as session:
        for row in repo.unposted_decisions(session):
            repo.claim_for_posting(session, row.id)
    page = client.get(url).text
    assert "Undo before the next poll" not in page
    assert "v3 refused and signed" not in page


def test_undo_is_not_offered_once_something_ran_under_the_decision(client, engine,
                                                                   monkeypatch):
    pending = workflow_id(engine, "gwas-qc", "1.4.0")
    post(client, f"/ui/_/workflows/{pending}/approve")
    with db.writing(engine) as session:
        row = repo.get_workflow_version(session, pending)
        repo.add_request(
            session, request_id="req_ran", study_id=row.study_id, study_version=2,
            study_digest="sha256:s", workflow_digest=row.digest, member="m-01",
            node=mock.NODE, datasets=[], not_after=repo.utcnow(),
            payload_digest="sha256:ran", raw=b"{}")
        repo.set_verdict(session, "req_ran", Verdict.ACCEPTED)
    text = client.get(f"/ui/workflows/{pending}").text
    assert "Approved and signed." in text
    assert "Undo before the next poll" not in text


def test_seeded_history_has_left_the_node(client, engine):
    with db.reading(engine) as session:
        assert repo.unposted_decisions(session) == []
    assert "Undo before the next poll" not in client.get(
        f"/ui/studies/{mock.MICROBIOME}").text


def test_a_stale_undo_is_refused_in_place(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse")
    path = undo_of(client.get(response.headers["hx-redirect"]).text)
    with db.writing(engine) as session:
        for row in repo.unposted_decisions(session):
            repo.mark_posted(session, row.id)
    refused = post(client, path)
    assert refused.status_code == 409
    assert "already been sent" in refused.text


def test_a_second_click_on_a_decided_version_is_refused(client, engine, v3):
    post(client, f"/ui/_/inbox/study/{v3}/refuse")
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse")
    assert response.status_code == 409 and "already decided" in response.text


def test_no_trail_no_decision(client, engine, v3, monkeypatch, tmp_path):
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(tmp_path / "no" / "such" / "file"))
    response = post(client, f"/ui/_/inbox/study/{v3}/approve", dac_confirmed="on")
    assert response.status_code == 503
    assert "nothing was decided" in response.text
    assert state_of(engine, mock.CARDIO, 3) == "pending"


def test_a_post_without_the_token_changes_nothing(client, engine, v3):
    client.get("/ui/inbox")
    assert client.post(f"/ui/_/inbox/study/{v3}/approve",
                       data={"dac_confirmed": "on"}).status_code == 403
    assert state_of(engine, mock.CARDIO, 3) == "pending"


def test_an_unknown_version_is_a_404(client):
    assert client.get("/ui/inbox/study/99999").status_code == 404
    assert post(client, "/ui/_/inbox/study/99999/refuse").status_code == 404


def test_a_get_changes_nothing(client, engine, v3):
    with db.reading(engine) as session:
        before = len(session.exec(select(Decision)).all())
    client.get(f"/ui/inbox/study/{v3}")
    client.get(f"/ui/_/inbox/study/{v3}")
    with db.reading(engine) as session:
        assert len(session.exec(select(Decision)).all()) == before


def test_a_hostile_comment_is_text(client, engine, v3):
    response = post(client, f"/ui/_/inbox/study/{v3}/refuse", comment="<b>x</b>")
    with db.reading(engine) as session:
        assert repo.decisions_about(session, SubjectType.STUDY_VERSION,
                                    v3)[0].comment == "<b>x</b>"
    assert "<b>x" not in client.get(response.headers["hx-redirect"]).text


def test_a_hostile_goal_is_text(engine):
    with db.writing(engine) as session:
        repo.add_study_version(
            session, study_id="evil", version=1, digest="sha256:e", raw=b"{}",
            goal="<script>alert(1)</script>", proposer="x", members=[], nodes=[],
            datasets=[], output_rules={})
    text = client_for(settings(), bind=engine).get("/ui/inbox").text
    assert "<script>alert" not in text and "&lt;script&gt;" in text


def test_a_version_that_lists_no_members_cannot_be_approved(engine):
    with db.writing(engine) as session:
        row = repo.add_study_version(
            session, study_id="empty", version=1, digest="sha256:e", raw=b"{}",
            goal="Empty", proposer="x", members=[], nodes=[], datasets=[],
            output_rules={})
    text = client_for(settings(), bind=engine).get(f"/ui/inbox/study/{row.id}").text
    assert "This version lists no members." in text
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*>Approve", text)


def test_an_older_version_arriving_late_is_superseded_and_not_offered(engine):
    with db.writing(engine) as session:
        v2 = repo.study_version_named(session, mock.CARDIO, 2)
        repo.withdraw_study_version(session, v2.id)
        v3 = repo.study_version_named(session, mock.CARDIO, 3).id
        approved = repo.approve_study_version(session, v3)
        assert approved is None
        older = repo.add_study_version(
            session, study_id=mock.CARDIO, version=0, digest="sha256:o", raw=b"{}",
            goal="old", proposer="m-01", members=[{"member": "m-01"}], nodes=[],
            datasets=[], output_rules={})
        repo.add_member_key(session, study_version_id=older.id, member="m-01",
                            ls_aai_id="a@b", signing_key={}, result_key="age1",
                            binding_token="t")
        session.flush()
        repo.mark_identity_verified(session, older.id, "m-01", {"sub": "a@b"})
        assert older.state == "superseded"
    text = client_for(settings(), bind=engine).get(f"/ui/inbox/study/{older.id}").text
    assert "Superseded" in text
    assert not re.search(r"<button[^>]*>Approve", text)


# Studies (02)


def test_the_study_list_puts_approved_first(client):
    text = client.get("/ui/studies").text
    names = re.findall(r'text-\[15px\] font-medium[^>]*>([^<]+)<', text)
    assert names == ["cardio-pt · CARDIO-PT cohort · v2",
                     "sepsis-biomarkers · Sepsis biomarkers · v1",
                     "microbiome-pilot · Microbiome pilot · v1"]


def test_the_cards_of_the_mock(client):
    text = client.get("/ui/studies").text
    assert "3 workflows approved · 1 waiting · 14 requests" in text
    assert "1 workflow approved · 6 requests" in text, "nothing waiting, nothing said"
    assert "v3 in Inbox" in text and ", kept for the audit log" in text


def test_a_study_only_waiting_is_not_listed(client):
    assert "rare-variants-pilot" not in client.get("/ui/studies").text


def test_no_studies_says_so(tmp_path):
    bind = fresh_database(tmp_path)[1]
    text = client_for(settings(), bind=bind).get("/ui/studies").text
    assert "No study has been approved on this node yet." in text


def test_the_header_of_an_approved_study(client):
    text = client.get(f"/ui/studies/{mock.CARDIO}").text
    assert re.search(r"Approved \d+ \w{3} by j\.silva", text)
    assert ">Withdraw approval</button>" in text


def test_the_overview_of_the_mock(client):
    text = client.get(f"/ui/studies/{mock.CARDIO}").text
    assert "Ana Costa (proposer), Rui Matos" in text
    assert "✓ identities verified" in text
    assert "min_count: 10" in text


def test_versions_show_superseded_and_who_decided(client):
    text = client.get(f"/ui/studies/{mock.CARDIO}").text
    assert "Waiting in Inbox" in text
    assert "Superseded" in text
    assert "automatic, when v2 was approved by j.silva" in text


def test_the_tabs_load_in_place(client):
    part = client.get(f"/ui/_/studies/{mock.CARDIO}/tab/workflows").text
    assert "<html" not in part and 'role="tablist"' in part
    assert 'aria-selected="true"' in part and "Parameter values" in part


def test_a_tab_works_as_a_page_too(client):
    assert "Parameter values" in client.get(
        f"/ui/studies/{mock.CARDIO}?tab=workflows").text


def test_an_unknown_tab_or_study_is_a_404(client):
    assert client.get(f"/ui/_/studies/{mock.CARDIO}/tab/nonsense").status_code == 404
    assert client.get("/ui/studies/nonsense").status_code == 404


def test_the_workflows_tab_of_the_mock(client):
    text = client.get(f"/ui/_/studies/{mock.CARDIO}/tab/workflows").text
    assert text.count("<tr>") == 5
    assert "4be1…90c3" in text and "2a77…c5e8" in text and "6d02…a1f3" in text
    assert "New version" in text and ">Review</a>" in text and ">Withdraw</button>" in text
    assert "maf_min 0.01" in text and "maf_min 0.05" in text


def test_the_requests_tab_of_the_mock(client):
    text = client.get(f"/ui/_/studies/{mock.CARDIO}/tab/requests").text
    assert "Refused · workflow not approved" in text
    assert "Refused · expired" in text
    assert "Done · in review" in text and "Done · sent" in text
    assert "Rui Matos" in text and "gwas-qc · 1.3.2" in text


def test_twenty_requests_a_page_with_the_rest_behind_a_button(client, engine):
    with db.writing(engine) as session:
        for index in range(10):
            repo.add_request(
                session, request_id=f"req_x{index}", study_id=mock.CARDIO,
                study_version=2, study_digest="sha256:s", workflow_digest="sha256:w",
                member="m-01", node=mock.NODE, datasets=[],
                not_after=repo.utcnow(), payload_digest=f"sha256:{index}", raw=b"{}",
                now=repo.utcnow() - timedelta(days=30, hours=index))
            repo.set_verdict(session, f"req_x{index}", Verdict.ACCEPTED)
    text = client.get(f"/ui/_/studies/{mock.CARDIO}/tab/requests").text
    assert text.count('<td class="mono">') == 20
    assert "Show 4 older" in text
    more = client.get(f"/ui/_/studies/{mock.CARDIO}/requests?offset=20").text
    assert more.count('<td class="mono">') == 4
    assert "older" not in more


def test_withdrawing_asks_in_the_page_first(client, engine):
    v2 = version_id(engine, mock.CARDIO, 2)
    part = client.get(f"/ui/_/studies/version/{v2}/withdraw").text
    assert ">Withdraw and sign</button>" in part
    assert state_of(engine, mock.CARDIO, 2) == "approved"


def test_no_browser_dialog_is_used(client):
    text = client.get(f"/ui/studies/{mock.CARDIO}").text
    assert "hx-confirm" not in text and "confirm(" not in text


def test_withdrawing_and_undoing_a_study(client, engine):
    with db.writing(engine) as session:
        fresh = repo.add_study_version(
            session, study_id="undoable", version=1, digest="sha256:u", raw=b"{}",
            goal="Undoable", proposer="m-01", members=[{"member": "m-01"}], nodes=[],
            datasets=[], output_rules={})
        repo.add_member_key(session, study_version_id=fresh.id, member="m-01",
                            ls_aai_id="a@b", signing_key={}, result_key="age1",
                            binding_token="t")
        session.flush()
        repo.mark_identity_verified(session, fresh.id, "m-01", {"sub": "a@b"})
        repo.approve_study_version(session, fresh.id)
    response = post(client, f"/ui/_/studies/version/{fresh.id}/withdraw")
    page = client.get(response.headers["hx-redirect"]).text
    assert "v1 withdrawn and signed." in page
    post(client, undo_of(page))
    assert state_of(engine, "undoable", 1) == "approved"


def test_a_study_something_ran_under_can_be_withdrawn(client, engine):
    v2 = version_id(engine, mock.CARDIO, 2)
    response = post(client, f"/ui/_/studies/version/{v2}/withdraw")
    assert state_of(engine, mock.CARDIO, 2) == "withdrawn"
    assert "kept for the audit log" in client.get(response.headers["hx-redirect"]).text


def test_a_withdrawn_workflow_can_be_undone_from_the_tab(client, engine):
    with db.reading(engine) as session:
        version = repo.approved_version(session, mock.CARDIO)
        row = next(item for item in repo.workflow_versions(session, version.id)
                   if item.state == "approved")
    response = post(client, f"/ui/_/workflows/{row.id}/withdraw")
    assert response.headers["hx-redirect"].endswith("?tab=workflows")
    text = client.get(response.headers["hx-redirect"]).text
    assert "Undo before the next poll" in text
    post(client, undo_of(text))
    with db.reading(engine) as session:
        assert repo.get_workflow_version(session, row.id).state == "approved"


# Workflow version (03)


@pytest.fixture
def pending(engine):
    return workflow_id(engine, "gwas-qc", "1.4.0")


def test_the_workflow_page_of_the_mock(client, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert "Workflow version · waiting for this node" in text
    assert "Compared with 1.3.2 (approved)" in text
    assert "sha256:4be1…90c3" in text
    assert "cardio-pt · CARDIO-PT cohort · v2" in text


def test_the_page_shows_tools_as_repo_at_digest_and_no_image(client, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert "biochef/bcftools@sha256:0e5f…92dd" in text
    assert "image" not in re.sub(r"<[^>]+>", "", text).lower().replace("img", "")


def test_the_changed_step_is_selected_and_tagged(client, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert text.count("tool changed") == 1 and text.count(">unchanged<") == 3
    assert "Step · qc_variants" in text
    assert "Keeps variants above the MAF and HWE thresholds." in text
    assert "maf_min 0.01" in text and "hwe_p 1e-6" in text
    assert "✓ sign/v1 signature from the hub" in text


def test_a_step_loads_in_place(client, pending):
    part = client.get(f"/ui/_/workflows/{pending}/step/0").text
    assert "<html" not in part and "Step · qc_samples" in part


def test_the_graph_runs_from_the_dataset_to_egress(client, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert text.index(">Dataset<") < text.index("qc_samples") < text.index("Output rules")


def test_what_changed_since_the_approved_version(client, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert "What changed since 1.3.2" in text
    assert "− qc_variants  biochef/bcftools  sha256:77c4…b1aa" in text
    assert "+ qc_variants  biochef/bcftools  sha256:0e5f…92dd" in text
    assert "parameter values unchanged" in text


def test_approving_a_workflow(client, engine, pending):
    text = client.get(f"/ui/workflows/{pending}").text
    assert "Approve 1.4.0 and sign" in text
    assert "1.3.2 stays approved. Requests name the hash they want" in text
    response = post(client, f"/ui/_/workflows/{pending}/approve")
    page = client.get(response.headers["hx-redirect"]).text
    assert ("Approved and signed. gwas-qc · 1.4.0 is now listed under "
            "cardio-pt · CARDIO-PT cohort · v2.") in page
    assert "Undo before the next poll" in page


def test_refusing_a_workflow(client, pending):
    response = post(client, f"/ui/_/workflows/{pending}/refuse")
    assert "Refused and signed." in client.get(response.headers["hx-redirect"]).text


def test_a_workflow_under_an_unapproved_study_cannot_be_approved(engine, client):
    with db.writing(engine) as session:
        v3 = repo.study_version_named(session, mock.CARDIO, 3)
        row = repo.add_workflow_version(
            session, study_version_id=v3.id, digest="sha256:" + "ab" * 32, raw=b"{}",
            steps=[{"id": "only", "repo": "biochef/x", "digest": "sha256:" + "cd" * 32}])
    text = client.get(f"/ui/workflows/{row.id}").text
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*>Approve", text)
    assert "can be approved only under an approved study version" in text


def test_a_workflow_with_no_library_name_shows_its_digest(engine, client):
    digest = "sha256:" + "ab" * 32
    with db.writing(engine) as session:
        v2 = repo.study_version_named(session, mock.CARDIO, 2)
        row = repo.add_workflow_version(
            session, study_version_id=v2.id, digest=digest, raw=b"{}",
            steps=[{"id": "only", "repo": "biochef/x", "digest": "sha256:" + "cd" * 32}])
    text = client.get(f"/ui/workflows/{row.id}").text
    assert ">sha256:abab…abab</h1>" in text
    assert "There is no approved version of this workflow to compare with." in text
    assert "No description came with this step." in text
    assert "No signature recorded" in text


def test_the_source_page_shows_the_document_as_received(client, engine, pending):
    text = client.get(f"/ui/workflows/{pending}/source").text
    assert "recipe.json, as received" in text
    assert "biochef/plink2@sha256:" in text
    with db.reading(engine) as session:
        raw = repo.get_workflow_version(session, pending).raw
    assert json.loads(raw)["nodes"][0]["id"] == "qc_samples"


def test_a_tool_not_on_this_machine_is_said_so_and_nothing_is_fetched(client, pending,
                                                                      monkeypatch):
    import convert
    monkeypatch.setattr(convert, "fetch_tool",
                        lambda *a: pytest.fail("the page reached the registry"))
    text = client.get(f"/ui/workflows/{pending}/source").text
    assert "is not in this node&#39;s tool cache" in text


def test_the_snakefile_is_built_from_the_cache_and_marked_so(client, pending,
                                                             monkeypatch, tmp_path):
    import convert
    bundle = {"bin": "tool", "io": {"inputs": [], "outputs": []}, "parameters": []}
    for name in ("qc_samples", "qc_variants", "assoc", "summarise"):
        (tmp_path / name.split("-")[0]).mkdir()
        (tmp_path / name / "bundle.json").write_text(json.dumps(bundle))
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path))
    text = client.get(f"/ui/workflows/{pending}/source").text
    assert "rule all:" in text
    assert "may not be the versions this workflow pins" in text


def test_a_document_that_does_not_parse_is_said_so(engine, client):
    with db.writing(engine) as session:
        v2 = repo.study_version_named(session, mock.CARDIO, 2)
        row = repo.add_workflow_version(
            session, study_version_id=v2.id, digest="sha256:bad", raw=b"not json",
            steps=[])
    response = client.get(f"/ui/workflows/{row.id}/source")
    assert response.status_code == 200
    assert "cannot be converted" in response.text


def test_an_unknown_workflow_is_a_404(client):
    assert client.get("/ui/workflows/99999").status_code == 404


def test_every_page_of_the_seed_loads_only_from_the_agent(client, engine, pending):
    pages = ["/ui/inbox", "/ui/studies", f"/ui/studies/{mock.CARDIO}?tab=requests",
             f"/ui/workflows/{pending}", f"/ui/workflows/{pending}/source"]
    for path in pages:
        text = client.get(path).text
        assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", text), path
        assert 'style="' not in text and "onclick" not in text.lower()


def test_the_study_page_names_the_result_recipients(client):
    text = client.get(f"/ui/studies/{mock.MICROBIOME}").text
    assert "Result recipients" in text and "Submitter only" in text
