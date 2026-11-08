"""The Node screen (mock 07) and the audit check of section 8.7."""

import json
from datetime import timedelta

import pytest
from sqlmodel import select

import audit
from node import decisions, review
from store import db, repo, seed
from store.fixtures import mock
from store.models import Decision
from store.states import Action, SubjectType
from tests.store_support import NOW, add_version, fresh_database
from tests.ui_support import KEYS, client_for, dev_settings
from ui.views import events_without_decision


@pytest.fixture
def trail(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BIOCHEF_AUDIT_LOG", str(path))
    return path


@pytest.fixture
def engine(tmp_path, trail):
    bind = fresh_database(tmp_path)[1]
    seed.seed(bind, keys=KEYS)
    return bind


def web(engine, **settings):
    return client_for(dev_settings(node_name=mock.NODE, **settings), bind=engine)


def test_the_four_cards(engine):
    text = web(engine).get("/ui/node").text
    assert "Ready" in text and "Up to date" in text and "5 nodes" in text
    assert "Online" in text and "Last poll" in text
    assert "subprocess" in text


def test_fingerprints_are_short_with_the_full_ones_behind_a_toggle(engine):
    text = web(engine).get("/ui/node").text
    print_ = KEYS.signing_fingerprint
    assert f"ed25519:{print_[8:12]}…{print_[-4:]}" in text
    assert print_ in text and "Show fingerprints" in text
    assert KEYS.encryption_fingerprint in text


def test_no_private_key_reaches_the_page(engine):
    assert "PRIVATE" not in web(engine).get("/ui/node").text


def test_isolation_is_off_in_warn_colours(engine):
    text = web(engine, runner="apptainer").get("/ui/node").text
    assert '<span class="text-[13px] font-medium text-warn">Network isolation: off</span>' in text
    assert "steps run without network" not in text


def test_isolation_is_on_only_when_the_runner_says_so(engine):
    text = web(engine, runner="apptainer", network_isolated=True).get("/ui/node").text
    assert "Network isolation: on" in text and "Network isolation: off" not in text


def test_busy_slots_are_counted_from_the_store(engine):
    assert "1 of 4 slots busy" in web(engine).get("/ui/node").text


def test_the_settings(engine):
    text = web(engine, egress_mode="manual", bindings_from_ui=True, local_root="/data",
               signing_mode="strict").get("/ui/node").text
    assert "Review in the agent" in text and "through the TRE airlock" in text
    assert "Config file and this page" in text and "/data" in text
    assert "Kept 1 hour, at most 32 runs, then deleted" in text
    assert "strict · unsigned tools never run" in text


def test_the_other_settings(engine):
    text = web(engine).get("/ui/node").text
    assert "Through the TRE airlock" in text and "Config file only" in text
    assert "off · tool signatures are not checked" in text


def test_a_silent_coordinator_goes_offline_in_bad_colours(engine):
    with db.writing(engine) as session:
        repo.record_poll(session, ok=True, now=repo.utcnow() - timedelta(minutes=10))
    text = web(engine).get("/ui/node").text
    assert "Offline since" in text and "text-bad" in text


def test_three_poll_intervals_is_the_limit(engine):
    with db.writing(engine) as session:
        repo.record_poll(session, ok=True, now=repo.utcnow() - timedelta(seconds=80))
    assert "Online" in web(engine).get("/ui/node").text


def test_a_node_that_never_polled_says_so(tmp_path, trail):
    (tmp_path / "e").mkdir()
    bind = fresh_database(tmp_path / "e")[1]
    text = web(bind).get("/ui/node").text
    assert "Not connected yet" in text and "No signed node list yet" in text
    assert "Nothing in the audit trail yet." in text


def decide(session, user="j.silva", subject_id=7, action=Action.REFUSE, at=NOW):
    return decisions.decide(session, KEYS, node="n",
                            subject_type=SubjectType.WORKFLOW_VERSION,
                            subject_id=subject_id, action=action, user=user, at=at)


def test_the_audit_log_shows_the_newest_first(engine, trail):
    with db.writing(engine) as session:
        decide(session, subject_id=1)
        decide(session, subject_id=2, action=Action.APPROVE)
    text = web(engine).get("/ui/node").text
    assert text.index("Workflow version approved") < text.index("Workflow version refused")
    assert "node key" in text


def test_only_the_newest_50_are_shown(engine, trail):
    for index in range(60):
        audit.record("run.state", run_id=f"run_{index}", state="RUNNING")
    text = web(engine).get("/ui/node").text
    assert "run_59" in text and "run_10<" in text and "run_9<" not in text


def test_every_kind_of_event_has_words(engine, trail):
    audit.record("node.request.checked", request_id="r1", verdict="refused",
                 failed_check="expired")
    audit.record("node.request.checked", request_id="r2", verdict="joined")
    audit.record("node.request.checked", request_id="r3", verdict="accepted")
    text = web(engine).get("/ui/node").text
    assert "Request refused, expired" in text
    assert "Repeated request joined existing run" in text and "Request accepted" in text


def test_a_bad_line_in_the_trail_is_shown_not_hidden(engine, trail):
    audit.record("run.state", run_id="r1", state="RUNNING")
    with open(trail, "a") as handle:
        handle.write("not json\n")
    text = web(engine).get("/ui/node").text
    assert "Unreadable line 2" in text and '<tr class="bg-bad-soft">' in text


def test_the_export_is_the_file(engine, trail):
    audit.record("run.state", run_id="r1", state="RUNNING")
    response = web(engine).get("/ui/node/audit.jsonl")
    assert "attachment" in response.headers["content-disposition"]
    assert json.loads(response.text)["run_id"] == "r1"


def test_a_trail_that_does_not_exist_yet_is_a_404(engine):
    assert web(engine).get("/ui/node/audit.jsonl").status_code == 404


def test_the_api_has_no_audit_route():
    import main
    paths = [route.path for route in main.app.routes]
    assert not [path for path in paths if "audit" in path]


# The audit check


def orphans(engine):
    with db.reading(engine) as session:
        return events_without_decision(session, audit.read())


@pytest.fixture
def bare(tmp_path, trail):
    return fresh_database(tmp_path)[1]


def test_a_healthy_trail_has_nothing_to_show(engine, trail):
    with db.writing(engine) as session:
        decide(session)
    assert orphans(engine) == []
    assert "✓ Every decision event in the audit trail has its decision." in (
        web(engine).get("/ui/node").text)


def test_a_failed_commit_leaves_an_event_with_no_decision(bare, trail):
    with pytest.raises(RuntimeError):
        with db.writing(bare) as session:
            decide(session)
            raise RuntimeError("the commit failed")
    assert len(orphans(bare)) == 1


def test_the_screen_lists_it_in_warn_colours(bare, trail):
    with pytest.raises(RuntimeError):
        with db.writing(bare) as session:
            decide(session, user="a.costa")
            raise RuntimeError("the commit failed")
    text = web(bare).get("/ui/node").text
    assert "1 audit event with no decision behind it." in text
    assert '<tr class="bg-warn-soft">' in text and "a.costa" in text


def test_an_undone_decision_is_accounted_for(bare, trail):
    with db.writing(bare) as session:
        version = add_version(session).id
        decision = review.approve_study(session, KEYS, node="n", version_id=version,
                                         user="j.silva", dac_confirmed=True, now=NOW).id
    with db.writing(bare) as session:
        review.undo(session, decision, user="j.silva")
    assert orphans(bare) == []


def test_an_id_reused_after_a_rollback_does_not_hide_the_event(bare, trail):
    with pytest.raises(RuntimeError):
        with db.writing(bare) as session:
            first = decide(session, subject_id=1).id
            raise RuntimeError("the commit failed")
    with db.writing(bare) as session:
        second = decide(session, subject_id=2).id
    assert first == second
    found = orphans(bare)
    assert [event["subject_id"] for event in found] == ["1"]


def test_an_undo_matches_the_latest_event_with_that_id(bare, trail):
    """A rolled back event, then a real decision with the same id, then its undo."""
    with pytest.raises(RuntimeError):
        with db.writing(bare) as session:
            decide(session, subject_id=1)
            raise RuntimeError("the commit failed")
    with db.writing(bare) as session:
        decision = decide(session, subject_id=2).id
    with db.writing(bare) as session:
        decisions.undo(session, decision, user="j.silva")
    assert [event["subject_id"] for event in orphans(bare)] == ["1"]


def test_the_check_only_reads(bare, trail):
    with pytest.raises(RuntimeError):
        with db.writing(bare) as session:
            decide(session)
            raise RuntimeError("the commit failed")
    before = trail.read_text()
    web(bare).get("/ui/node")
    assert trail.read_text() == before


# What the runner guarantees, and its network check at start (section 14.1)

ALL_ENFORCED = (("network", "enforced"), ("filesystem", "enforced"),
                ("environment", "enforced"))


def test_the_card_lists_the_three_guarantees(engine):
    text = web(engine).get("/ui/node").text
    for aspect in ("Network:", "Filesystem:", "Environment:"):
        assert aspect in text
    assert text.count('<span class="font-medium text-warn">none</span>') == 3


def test_enforced_guarantees_are_in_ok_colours(engine):
    text = web(engine, runner="podman", network_isolated=True,
               guarantees=ALL_ENFORCED).get("/ui/node").text
    assert text.count('<span class="text-ok">enforced by the agent</span>') == 3


def test_an_attested_guarantee_says_it_is_not_verified(engine):
    text = web(engine, guarantees=(("network", "attested"), ("filesystem", "none"),
                                   ("environment", "none"))).get("/ui/node").text
    assert ('<span class="font-medium text-warn">left to the backend, not verified'
            '</span>') in text


def test_no_check_is_shown_for_a_runner_that_claims_no_isolation(engine):
    assert "at start" not in web(engine).get("/ui/node").text


def test_an_isolating_runner_not_checked_yet_says_so(engine):
    text = web(engine, network_isolated=True, guarantees=ALL_ENFORCED).get(
        "/ui/node").text
    assert '<span class="text-[13px] text-muted">Not checked at start yet</span>' in text


def test_a_passed_check_is_shown(engine):
    with db.writing(engine) as session:
        repo.record_network_check(session, ok=True, detail="no route or address but loopback",
                                  now=NOW)
    text = web(engine, network_isolated=True, guarantees=ALL_ENFORCED).get(
        "/ui/node").text
    assert "text-ok\">Checked at start" in text and "no route or address but loopback" in text


def test_a_failed_check_is_shown_in_bad_colours(engine):
    with db.writing(engine) as session:
        repo.record_network_check(session, ok=False, detail="a step saw 1 routes or addresses besides loopback",
                                  now=NOW)
    text = web(engine, network_isolated=True, guarantees=ALL_ENFORCED).get(
        "/ui/node").text
    assert 'font-medium text-bad">Check at start failed' in text
    assert "a step saw 1 routes or addresses besides loopback" in text


@pytest.mark.parametrize("required,words", [
    (True, "Required: a runner the agent does not isolate is refused"),
    (False, "Not required")])
def test_the_rule_is_among_the_settings(engine, required, words):
    text = web(engine, require_network_isolation=required).get("/ui/node").text
    assert f"<span>{words}</span>" in text

# --------------------------------------------------------------------------
# the words of the trail: names a person knows, for every kind of event



def audit_text(engine):
    page = web(engine).get("/ui/node").text
    start = page.index("Append only")
    return page[start:]


def test_a_version_is_named_in_the_trail_and_not_numbered(engine, trail):
    with db.reading(engine) as session:
        study = repo.get_study_version(session, 3)
        workflow = repo.get_workflow_version(session, 4)
    with db.writing(engine) as session:
        review.approve_study(session, KEYS, node="n", version_id=study.id,
                             user="j.silva", dac_confirmed=True, now=NOW)
        decisions.decide(session, KEYS, node="n", subject_type=SubjectType.WORKFLOW_VERSION,
                         subject_id=workflow.id, action=Action.APPROVE, user="j.silva", at=NOW)
    text = audit_text(engine)
    assert "cardio-pt · CARDIO-PT cohort · v3" in text
    assert "gwas-qc · 1.4.0" in text
    assert f'<td class="mono">{study.id}</td>' not in text
    assert f'<td class="mono">{workflow.id}</td>' not in text


def test_a_version_the_trail_names_that_is_gone_is_shown_by_its_number(engine, trail):
    with db.writing(engine) as session:
        decide(session, subject_id=9999, action=Action.APPROVE)
    assert '<td class="mono">9999</td>' in audit_text(engine)


def test_an_undo_names_the_subject_it_took_back(bare, trail):
    with db.writing(bare) as session:
        version = add_version(session)
        decision = review.approve_study(session, KEYS, node="n", version_id=version.id,
                                         user="j.silva", dac_confirmed=True, now=NOW).id
    with db.writing(bare) as session:
        review.undo(session, decision, user="j.silva")
    text = audit_text(bare)
    assert "Decision undone" in text
    assert text.count("st-01 · A study · v1") == 2, "the decision and the undo name it"
    assert f'<td class="mono">decision {decision}</td>' not in text


def test_an_undo_with_no_decision_event_before_it_still_has_a_subject(bare, trail):
    audit.record("node.decision.undone", decision_id=41, user="j.silva")
    assert '<td class="mono">decision 41</td>' in audit_text(bare)


@pytest.mark.parametrize("name,words", [
    ("node.egress.notice", "Notice of a withheld result signed"),
    ("node.something.new_kind", "Node something new kind"),
    ("a_plain_name", "A plain name"),
])
def test_every_event_is_in_words(engine, trail, name, words):
    audit.record(name, run_id="run_x")
    text = audit_text(engine)
    assert words in text
    assert f"<td>{name}</td>" not in text


def test_a_demo_trail_has_no_raw_event_names(engine, trail):
    written = seed.write_audit(engine)
    assert written > 10
    text = audit_text(engine)
    for raw in ("node.decision<", "node.request.checked<", "run.state<", ">node."):
        assert raw not in text, raw
    assert "Study version approved" in text and "Request refused" in text
    assert "Run COMPLETE" in text and "Run RUNNING" in text


def test_a_demo_trail_passes_its_own_check(engine, trail):
    seed.write_audit(engine)
    assert orphans(engine) == []
    assert "✓ Every decision event in the audit trail has its decision." in (
        web(engine).get("/ui/node").text)


def test_a_demo_trail_is_in_time_order_and_every_decision_has_one_event(engine, trail):
    seed.write_audit(engine)
    events = audit.read()
    stamps = [event["at"] for event in events]
    assert stamps == sorted(stamps)
    with db.reading(engine) as session:
        ids = {row.id for row in session.exec(select(Decision))}
    assert sorted(e["decision_id"] for e in events if e["event"] == "node.decision") == \
        sorted(ids)


def test_no_trail_means_no_demo_events(engine, monkeypatch):
    monkeypatch.delenv("BIOCHEF_AUDIT_LOG")
    assert seed.write_audit(engine) == 0


def test_a_demo_event_carries_the_time_of_what_it_records(engine, trail):
    seed.write_audit(engine)
    approvals = [e for e in audit.read()
                 if e["event"] == "node.decision" and e["action"] == "approve"
                 and e["subject_type"] == "study_version"]
    assert approvals, "the seed approves study versions"
    with db.reading(engine) as session:
        for event in approvals:
            row = session.get(Decision, event["decision_id"])
            assert event["at"] == row.at.isoformat()
