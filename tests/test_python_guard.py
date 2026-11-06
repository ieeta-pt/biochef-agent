"""run.sh says which Python it needs, before pip buries the answer."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parent.parent / "run.sh").read_text()
GUARD = re.search(r"python -c '([^']+)' \|\|", SCRIPT)


def accepts(version):
    """The script's own check, on an interpreter that claims this version."""
    claim = f"import sys; sys.version_info = {version + (0,)}; "
    return subprocess.run([sys.executable, "-c", claim + GUARD.group(1)]).returncode == 0


@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13)])
def test_the_versions_ci_tests_are_accepted(version):
    assert accepts(version)


@pytest.mark.parametrize("version", [(3, 9), (3, 10), (3, 14), (3, 15), (4, 0)])
def test_any_other_version_is_refused(version):
    assert not accepts(version)


def test_the_refusal_names_the_versions_to_use():
    assert "This needs Python 3.11, 3.12 or 3.13" in SCRIPT


def test_the_check_comes_before_pip():
    assert SCRIPT.index(GUARD.group(0)) < SCRIPT.index("pip install")
