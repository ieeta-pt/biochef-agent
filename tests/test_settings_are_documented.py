"""Every setting the code reads is written down somewhere (#15).

Not a style rule. The README says "Configuration is by environment variable, and
example.env lists them", and that sentence was false: nine of the fourteen
variables the code reads appeared in neither, including the two that decide
whether a step runs on the host or in a container.

An undocumented switch is not a switch. Nobody turns on isolation they have not
been told exists, and the default is the unisolated one.

This test exists because the drift is invisible -- adding os.getenv is one line
and breaks nothing.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _variables_the_code_reads():
    found = set()
    for module in REPO_ROOT.glob("*.py"):
        found |= set(re.findall(r'getenv\(\s*"([A-Z][A-Z0-9_]*)"',
                                module.read_text()))
    return found


def test_the_code_reads_some_settings():
    """Guard against the scan silently matching nothing.

    If getenv were spelled differently, or the glob missed the modules, every
    assertion below would pass over an empty set and this file would be a
    decoration.
    """
    found = _variables_the_code_reads()
    assert len(found) >= 10, found
    assert "BIOCHEF_RUNNER" in found
    assert "REGISTRY_URL" in found


def test_every_setting_appears_in_the_readme():
    readme = (REPO_ROOT / "README.md").read_text()
    missing = sorted(v for v in _variables_the_code_reads() if v not in readme)
    assert not missing, f"read by the code, absent from README.md: {missing}"


def test_every_setting_appears_in_example_env():
    example = (REPO_ROOT / "example.env").read_text()
    missing = sorted(v for v in _variables_the_code_reads() if v not in example)
    assert not missing, f"read by the code, absent from example.env: {missing}"


def test_the_readme_says_which_runner_is_the_default():
    """Because the default is the one WITHOUT a container.

    An operator who does not know that runs every tool on the host as this
    service's user, and nothing in the response says so.
    """
    readme = (REPO_ROOT / "README.md").read_text()
    assert "BIOCHEF_RUNNER" in readme
    assert "subprocess" in readme and "apptainer" in readme
    assert "on the host" in readme


def test_the_readme_states_what_the_defaults_cost_in_memory():
    """Runs are held in memory, and "in memory" is not a number.

    At the defaults the logs alone reach MAX_RUNS x MAX_LOG_BYTES x two streams.
    An operator should be able to read that off the page rather than multiply it
    themselves, and the figure should stop being right if the defaults move.
    """
    import runs
    import steplogs

    expected_mib = runs.MAX_RUNS * steplogs.MAX_LOG_BYTES * 2 // (1024 * 1024)
    readme = (REPO_ROOT / "README.md").read_text()

    assert f"{expected_mib} MiB" in readme, (
        f"the README should say {expected_mib} MiB for the current defaults "
        f"(MAX_RUNS={runs.MAX_RUNS}, MAX_LOG_BYTES={steplogs.MAX_LOG_BYTES})"
    )
