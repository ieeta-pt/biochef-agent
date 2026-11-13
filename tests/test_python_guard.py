"""The start scripts say which Python they need, before pip buries the answer.

The range they accept is the one CI tests: the versions are read from the suite
matrix of ci.yml, so adding a version to one and not the other fails here.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(ROOT.glob("run*.sh"))
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
TESTED = [tuple(int(part) for part in version.split("."))
          for version in CI["jobs"]["suite"]["strategy"]["matrix"]["python"]]
GUARD = re.compile(r"python -c '([^']+)' \|\|")


def guard_of(script):
    found = GUARD.search(script.read_text())
    assert found, f"{script.name} has no Python version check"
    return found


def accepts(script, version):
    """The script's own check, on an interpreter that claims this version."""
    claim = f"import sys; sys.version_info = {version + (0,)}; "
    return subprocess.run([sys.executable, "-c",
                           claim + guard_of(script).group(1)]).returncode == 0


def outside(versions):
    """The versions next to the tested range, and a few further out."""
    floor, newest = min(versions), max(versions)
    return [(3, 9), (floor[0], floor[1] - 1), (newest[0], newest[1] + 1), (4, 0)]


def test_the_matrix_is_a_range_with_no_gap():
    minors = sorted(minor for major, minor in TESTED if major == 3)
    assert len(minors) == len(TESTED) and minors == list(range(minors[0], minors[-1] + 1))


def test_there_is_a_script_to_check():
    assert ROOT / "run.sh" in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("version", TESTED, ids=str)
def test_every_version_ci_tests_is_accepted(script, version):
    assert accepts(script, version)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("version", outside(TESTED), ids=str)
def test_any_other_version_is_refused(script, version):
    assert not accepts(script, version)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_refusal_names_the_versions_ci_tests(script):
    names = [f"{major}.{minor}" for major, minor in TESTED]
    listed = ", ".join(names[:-1]) + " or " + names[-1]
    assert f"This needs Python {listed}" in script.read_text()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_check_comes_after_activation_and_before_pip(script):
    """After activation, so a venv built on an older Python is caught too."""
    text = script.read_text()
    check = text.index(guard_of(script).group(0))
    assert text.index("source venv/bin/activate") < check < text.index("pip install")
