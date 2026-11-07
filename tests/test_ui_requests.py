"""The Requests screen (mock 04), as corrected in section 0.1."""

import re

import pytest

from store import repo, seed
from store.fixtures import mock
from tests.store_support import fresh_database
from tests.ui_support import KEYS, client_for, dev_settings


@pytest.fixture
def client(tmp_path):
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    return client_for(dev_settings(node_name=mock.NODE), bind=bind)


def ids(text):
    return re.findall(r'class="btn-link mono[^>]*>(req_\w+)<', text)


def test_everything_listed_is_inside_24_hours_newest_first(client):
    assert ids(client.get("/ui/requests").text) == [
        "req_8e90", "req_8f40", "req_8f33", "req_8f21", "req_7c03"]


def test_the_filters_and_their_counts(client):
    text = client.get("/ui/requests").text
    assert "All 5" in text and "Accepted 3" in text and "Refused 2" in text


def test_the_refused_filter(client):
    assert ids(client.get("/ui/requests?filter=refused").text) == ["req_8f40", "req_8f33"]


def test_an_unknown_filter_is_a_404(client):
    assert client.get("/ui/requests?filter=nonsense").status_code == 404


def test_the_verdicts_of_the_mock(client):
    text = client.get("/ui/requests").text
    for verdict in ("Refused · workflow not approved", "Refused · visa missing",
                    "Accepted · run RUNNING",
                    "Accepted · run COMPLETE", "Accepted · result sent"):
        assert verdict in text


def test_the_panel_lists_all_ten_checks(client):
    text = client.get("/ui/requests?selected=req_8f21").text
    assert text.count('<li class="flex gap-2') == 10
    assert "Addressed to this node" in text
    assert text.count("✓") >= 10


def panel(text):
    return text[text.index('aria-label="Checks"'):]


def test_a_refusal_marks_the_failure_and_what_was_not_reached(client):
    text = panel(client.get("/ui/requests?selected=req_8f40").text)
    assert text.count("· not reached") == 6
    assert "✗" in text
    assert text.index("Workflow hash approved") < text.index("✗") + 80
    assert "The refusal is signed with the node key" in text


def test_a_repeat_is_shown_on_the_request(client):
    text = panel(client.get("/ui/requests?selected=req_8f21").text)
    assert "Delivered again once" in text
    assert "The same request_id joined this run." in text
    assert text.count("· not reached") == 0


def test_a_request_never_repeated_says_nothing_about_it(client):
    assert "Delivered again" not in panel(
        client.get("/ui/requests?selected=req_7c03").text)


def test_a_selection_outside_the_filter_falls_back_to_the_first(client):
    text = client.get("/ui/requests?filter=refused&selected=req_8f21").text
    assert "<h2" in text and ">req_8f40</h2>" in text


def test_the_filter_and_selection_load_in_place(client):
    part = client.get("/ui/_/requests?filter=refused").text
    assert "<html" not in part and 'id="requests"' in part


def test_names_are_never_opaque_ids(client):
    text = client.get("/ui/requests").text
    assert not re.search(r"\bm-0\d\b", text) and "Rui Matos" in text


def test_a_verified_member_is_named_by_the_version_the_request_names(client):
    assert "Paulo Neves" in client.get("/ui/requests").text


def test_nothing_in_24_hours_says_so(tmp_path):
    bind = fresh_database(tmp_path)[1]
    assert "No requests in the last 24 hours." in client_for(
        dev_settings(), bind=bind).get("/ui/requests").text


def test_a_request_not_checked_yet_says_so(client):
    bind = client.app.state.bind
    from store import db
    with db.writing(bind) as session:
        repo.add_request(
            session, request_id="req_new", study_id=mock.CARDIO, study_version=2,
            study_digest="s", workflow_digest="sha256:w", member="m-01", node=mock.NODE,
            datasets=[], not_after=repo.utcnow(), payload_digest="sha256:n", raw=b"{}")
    text = client.get("/ui/requests?selected=req_new").text
    assert "Not checked yet" in text and "· not checked yet" in text


def test_a_request_for_a_version_this_node_does_not_hold(client):
    bind = client.app.state.bind
    from store import db
    with db.writing(bind) as session:
        repo.add_request(
            session, request_id="req_far", study_id="elsewhere", study_version=7,
            study_digest="s", workflow_digest="sha256:" + "ab" * 32, member="m-01",
            node=mock.NODE, datasets=[], not_after=repo.utcnow(),
            payload_digest="sha256:f", raw=b"{}")
    text = client.get("/ui/requests").text
    assert "elsewhere · v7" in text and "unknown member" in text


def test_a_visa_refusal_says_when_no_visa_can_match(tmp_path):
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    from store import db
    with db.writing(bind) as session:
        repo.dataset_binding(session, mock.SEPSIS_DATASET).visa_value = None
    browser = client_for(dev_settings(node_name=mock.NODE,
                                      required_visa="ControlledAccessGrants"), bind=bind)
    text = panel(browser.get("/ui/requests?selected=req_8f33").text)
    assert (f"{mock.SEPSIS_DATASET} has no visa value on this node, so no "
            "ControlledAccessGrants visa can match it.") in text


def visa_panel(tmp_path, value, required):
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    from store import db
    with db.writing(bind) as session:
        repo.dataset_binding(session, mock.SEPSIS_DATASET).visa_value = value
    browser = client_for(dev_settings(node_name=mock.NODE, required_visa=required),
                         bind=bind)
    return panel(browser.get("/ui/requests?selected=req_8f33").text)


def test_a_visa_refusal_with_a_value_set_adds_nothing(tmp_path):
    assert "has no visa value" not in visa_panel(
        tmp_path, "https://dac.example.org/x", "ControlledAccessGrants")


def test_no_reason_where_this_node_requires_no_visa(tmp_path):
    assert "has no visa value" not in visa_panel(tmp_path, None, None)
