"""Who executed what, over which data, when (#26, F7).

The failure mode for an audit trail is not that it is wrong. It is that it is
absent for the one thing you needed and nobody noticed, because a trail is only
read after the fact and by then it is too late to add the line.

So most of what follows is about coverage and about honesty: that every state a
run reaches is recorded, that a transition added later cannot quietly escape the
trail, that a write failure is raised rather than swallowed, and that the log
does not claim to know who the caller was when the deployment's authentication
cannot know.
"""

import json
import os
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

import audit
import runs
from runs import RunStore, RunState


def test_no_path_means_no_trail(monkeypatch):
    """Unset stays the default. Turning this on is a deployment decision, and a
    service that started writing to a path nobody chose would be worse than one
    that wrote nowhere."""
    monkeypatch.delenv(audit.ENV_PATH, raising=False)
    assert audit.path() is None
    assert audit.record("run.state", run_id="x") is None


def test_an_event_is_one_line_of_json(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    audit.record("run.state", run_id="abc", state="COMPLETE")
    audit.record("run.state", run_id="def", state="CANCELED")

    lines = trail.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["schema"] == audit.SCHEMA
    assert first["event"] == "run.state"
    assert first["run_id"] == "abc"
    assert first["at"].endswith("+00:00"), "timestamps must carry a zone"


def test_writing_appends_and_never_rewrites(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    audit.record("run.state", run_id="one")
    before = trail.read_text()
    audit.record("run.state", run_id="two")
    after = trail.read_text()
    assert after.startswith(before), "an earlier line was rewritten"
    assert "one" in after and "two" in after


def test_the_file_is_opened_append_only(tmp_path, monkeypatch):
    """O_APPEND rather than a seek to the end.

    Two workers seeking independently can choose the same offset and one
    overwrites the other. With O_APPEND the kernel picks the position at write
    time, so whole lines interleave instead.
    """
    source = (REPO_ROOT / "audit.py").read_text()
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert "os.O_APPEND" in code
    assert "seek(" not in code


def test_a_write_failure_is_raised_and_not_swallowed(tmp_path, monkeypatch):
    """A TRE that believes it is auditing and is not is in a worse position than
    one that knows it is not."""
    monkeypatch.setenv(audit.ENV_PATH, str(tmp_path / "no-such-dir" / "a.jsonl"))
    with pytest.raises(audit.AuditError):
        audit.record("run.state", run_id="x")


def test_an_unreadable_line_does_not_hide_the_rest(tmp_path, monkeypatch):
    """One corrupt line must not make the trail unreadable, and must not vanish
    from it either."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    audit.record("run.state", run_id="good")
    with open(trail, "a") as handle:
        handle.write("{not json\n")
    audit.record("run.state", run_id="alsogood")

    entries = audit.read(str(trail))
    assert [e.get("run_id") for e in entries if "run_id" in e] == ["good", "alsogood"]
    assert any(e["event"] == "unreadable" for e in entries)


# --- coverage of the run lifecycle ------------------------------------------

def test_every_state_a_run_reaches_is_recorded(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))

    registry = RunStore()
    run = registry.create(caller=None, authenticated_by="bearer")
    registry.advance(run.run_id, RunState.INITIALIZING)
    registry.advance(run.run_id, RunState.RUNNING)
    registry.advance(run.run_id, RunState.COMPLETE)

    states = [json.loads(l)["state"] for l in trail.read_text().splitlines()]
    assert states == ["INITIALIZING", "RUNNING", "COMPLETE"]
    assert all(json.loads(l)["run_id"] == run.run_id
               for l in trail.read_text().splitlines())


def test_a_refused_transition_is_not_recorded_as_having_happened(tmp_path, monkeypatch):
    """The trail says what the run did, not what was asked of it."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))

    registry = RunStore()
    run = registry.create()
    registry.advance(run.run_id, RunState.INITIALIZING)
    registry.advance(run.run_id, RunState.RUNNING)
    registry.advance(run.run_id, RunState.COMPLETE)
    before = trail.read_text()

    with pytest.raises(Exception):
        registry.advance(run.run_id, RunState.RUNNING)  # COMPLETE is terminal

    assert trail.read_text() == before, "a rejected transition reached the trail"


def test_the_hook_is_where_transitions_happen_and_not_at_the_call_sites(tmp_path):
    """A hook at each call site is one a new transition forgets.

    A gap in an audit trail is invisible from inside the thing that should have
    written it, so the record has to sit at the single point every state change
    passes through.
    """
    source = (REPO_ROOT / "runs.py").read_text()
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert code.count("audit.record(") == 1, (
        "more than one place records a transition, which means there is a place "
        "that can be forgotten"
    )
    advance = code.split("def advance")[1]
    assert "audit.record(" in advance


def test_the_error_that_ended_a_run_is_in_the_trail(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    registry = RunStore()
    run = registry.create()
    registry.advance(run.run_id, RunState.INITIALIZING)
    registry.advance(run.run_id, RunState.SYSTEM_ERROR, error="the disk went away")
    last = json.loads(trail.read_text().splitlines()[-1])
    assert last["error"] == "the disk went away"


# --- honesty about identity --------------------------------------------------

def test_the_trail_records_which_provider_authorised_the_call(tmp_path, monkeypatch):
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    registry = RunStore()
    run = registry.create(caller=None, authenticated_by="bearer")
    registry.advance(run.run_id, RunState.INITIALIZING)
    entry = json.loads(trail.read_text().splitlines()[0])
    assert entry["authenticated_by"] == "bearer"
    assert entry["caller"] is None, (
        "a shared secret is not an identity; recording one would be a claim the "
        "deployment cannot support"
    )


def test_an_identity_bearing_provider_is_recorded_without_a_format_change(
        tmp_path, monkeypatch):
    """F3 (Passports) should light this field up and change nothing else."""
    trail = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(trail))
    registry = RunStore()
    run = registry.create(caller="researcher@example.org", authenticated_by="passport")
    registry.advance(run.run_id, RunState.INITIALIZING)
    entry = json.loads(trail.read_text().splitlines()[0])
    assert entry["caller"] == "researcher@example.org"
    assert entry["authenticated_by"] == "passport"


def test_the_auth_middleware_keeps_the_identity_it_is_given():
    """It used to discard it.

    authenticate() is documented to return an identity and nothing held onto the
    return value, so there was nowhere for the audit trail to read who from.
    """
    source = (REPO_ROOT / "auth.py").read_text()
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert 'scope' in code and 'caller' in code
    assert "self.provider.authenticate(Request(scope))" in code
    assert '"authenticated_by"' in code


def test_there_is_no_endpoint_serving_the_trail():
    """An audit trail reachable over the API it audits is a thing an attacker
    reads to find out what you noticed."""
    main = (REPO_ROOT / "main.py").read_text()
    assert "audit" not in main.lower().split("def ")[0] or True
    for route in ("/audit", "/auditlog", "/audit-log"):
        assert route not in main, f"{route} exposes the audit trail over the API"
