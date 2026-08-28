"""Named profiles, and whether selecting one means anything (#17, E4).

There are sixteen settings and the two that decide how exposed a run is default
to the unguarded option: BIOCHEF_RUNNER runs tools on the host as this service's
user, and BIOCHEF_AUTH answers anybody. Neither default is wrong for a laptop.
Both are wrong for a machine holding data, and nothing in a response says which
one you are running.

The risk with a feature like this is that it becomes decoration: a profile that
sets a variable nothing reads, or one that an operator selects and which is then
quietly undone by a line in .env. Most of what follows is about those two.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

import profiles


def _settings_the_code_reads():
    found = set()
    for module in REPO_ROOT.glob("*.py"):
        found |= set(re.findall(r'getenv\(\s*"([A-Z][A-Z0-9_]*)"',
                                module.read_text()))
    return found


def test_the_scan_finds_settings():
    """Guard against the assertion below passing over an empty set."""
    found = _settings_the_code_reads()
    assert len(found) >= 10
    assert "BIOCHEF_RUNNER" in found


def test_no_profile_sets_a_variable_nothing_reads():
    """The way this feature would rot.

    A profile listing BIOCHEF_ISOLATION_LEVEL would look like it hardened
    something and would do nothing at all, and nobody would find out, because
    the startup line would print it just the same.
    """
    read = _settings_the_code_reads()
    for name, settings in profiles.PROFILES.items():
        unknown = sorted(k for k in settings if k not in read)
        assert not unknown, f"profile {name!r} sets {unknown}, which nothing reads"


def test_every_profile_is_documented():
    readme = (REPO_ROOT / "README.md").read_text()
    for name in profiles.PROFILES:
        assert f"`{name}`" in readme, f"profile {name!r} is not in README.md"


def test_no_profile_selected_changes_nothing():
    """Adding this must not change what an existing deployment does."""
    environ = {"BIOCHEF_RUNNER": "subprocess"}
    assert profiles.apply(environ) is None
    assert environ == {"BIOCHEF_RUNNER": "subprocess"}


def test_an_unknown_profile_refuses_to_start():
    """Rather than falling back to a default nobody chose."""
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.apply({"BIOCHEF_PROFILE": "production"})
    assert "production" in str(caught.value)
    assert "dev" in str(caught.value), "the message should say what IS valid"


def test_a_profile_fills_in_what_is_unset():
    environ = {"BIOCHEF_PROFILE": "tre"}
    result = profiles.apply(environ)
    assert environ["BIOCHEF_AUTH"] == "bearer"
    assert environ["BIOCHEF_RUNNER"] == "apptainer"
    assert ("BIOCHEF_AUTH", "bearer") in result.applied
    assert not result.overridden


def test_the_environment_wins_over_the_profile():
    """A configuration system that discards what an operator set is worse than
    one that makes them set more."""
    environ = {"BIOCHEF_PROFILE": "tre", "BIOCHEF_AUTH": "none"}
    result = profiles.apply(environ)
    assert environ["BIOCHEF_AUTH"] == "none"
    assert ("BIOCHEF_AUTH", "bearer", "none") in result.overridden


def test_an_override_is_reported_and_not_merely_counted():
    """The whole reason describe() exists.

    Selecting `tre` and having authentication off is a thing an operator may
    genuinely want and a thing they may do by accident, and the two are
    indistinguishable unless startup says which settings the profile did not get
    to decide.
    """
    environ = {"BIOCHEF_PROFILE": "tre", "BIOCHEF_AUTH": "none"}
    text = profiles.describe(profiles.apply(environ))
    assert "BIOCHEF_AUTH=none" in text
    assert "bearer" in text
    assert "wins" in text


def test_an_empty_setting_is_treated_as_unset():
    """`BIOCHEF_AUTH=` in a .env file is not a choice of provider."""
    environ = {"BIOCHEF_PROFILE": "tre", "BIOCHEF_AUTH": ""}
    profiles.apply(environ)
    assert environ["BIOCHEF_AUTH"] == "bearer"


def test_a_value_equal_to_the_profiles_own_is_not_reported_as_an_override():
    environ = {"BIOCHEF_PROFILE": "tre", "BIOCHEF_AUTH": "bearer"}
    result = profiles.apply(environ)
    assert not result.overridden


def test_dev_is_the_unguarded_one_and_says_so():
    """Stated rather than reached by leaving everything unset.

    The difference matters: an operator reading the startup line should see that
    this service is answering anybody because a profile said so, not because
    nobody decided.
    """
    environ = {"BIOCHEF_PROFILE": "dev"}
    profiles.apply(environ)
    assert environ["BIOCHEF_AUTH"] == "none"
    assert environ["BIOCHEF_RUNNER"] == "subprocess"


def test_server_and_tre_both_authenticate_and_containerise():
    for name in (profiles.SERVER, profiles.TRE):
        settings = profiles.PROFILES[name]
        assert settings["BIOCHEF_AUTH"] == "bearer"
        assert settings["BIOCHEF_RUNNER"] == "apptainer"
        assert "--contain" in settings["BIOCHEF_APPTAINER_ARGS"]


def test_tre_states_its_egress_allowlist_and_disclaims_enforcing_it():
    """E4 allows enforcement to be an external proxy at this stage.

    Which makes it important that the text does not read as though this service
    restricts anything. A dictionary in Python does not constrain outbound
    traffic, and documentation implying otherwise is the dangerous kind.
    """
    text = profiles.describe(profiles.apply({"BIOCHEF_PROFILE": "tre"}))
    assert "egress" in text
    assert "REGISTRY_URL" in text
    assert "does NOT enforce" in text

    other = profiles.describe(profiles.apply({"BIOCHEF_PROFILE": "server"}))
    assert "egress" not in other, "only tre makes a claim about egress"


def test_describe_says_when_nothing_was_selected():
    assert "No profile" in profiles.describe(None)
