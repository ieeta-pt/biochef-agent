"""How a workflow gets executed today, and what it would take to change it (#15).

Recorded before anything changes. E2 asks for "a second Runner provider", which
presumes a first one. There is no provider, and no seam: there is a module-level
function that the handler calls by name.

The consequence is visible in the suite itself. Three test files substitute
execution by monkeypatching `main.run_snakemake`, because reaching into a module
global is the only way to do it. That works for a test, which owns the process
and puts it back afterwards. It is not a mechanism a deployment can use to say
"run these steps in a container", which is what E2 needs.
"""

import inspect
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import main


def test_execution_is_a_module_level_function_not_a_provider():
    """No interface, no implementations, nothing to select between."""
    assert inspect.isfunction(main.run_snakemake)
    assert not Path(REPO_ROOT / "runner.py").exists()


def test_the_handler_names_the_function_directly():
    """So there is no point at which a different strategy could be supplied."""
    source = inspect.getsource(main.convert)
    assert "run_snakemake" in source
    assert "runner" not in source.lower()


def test_nothing_selects_how_a_workflow_runs():
    """Every other knob is configurable; how it executes is not.

    BIOCHEF_RUN_ROOT, BIOCHEF_RUN_TIMEOUT, BIOCHEF_KEEP_WORKSPACE and
    BIOCHEF_MAX_UPLOAD_BYTES are all read from the environment. There is no
    equivalent for the one thing E2 needs to vary.
    """
    settings = Path(REPO_ROOT / "main.py").read_text()
    assert "BIOCHEF_RUN_ROOT" in settings
    assert "BIOCHEF_RUN_TIMEOUT" in settings
    assert "BIOCHEF_RUNNER" not in settings


def test_substituting_execution_today_means_patching_a_module_global():
    """Which is what the suite already does.

    Named here so the count is a fact rather than an impression, and so this
    test starts failing if a seam appears and they stop needing to.

    test_run_directory.py is deliberately not in this list: it calls
    main.run_snakemake for real rather than replacing it, which is the whole
    point of that test.
    """
    needle = 'setattr(main, "run_' + 'snakemake"'   # split so this file is not a hit
    patchers = [
        p for p in sorted(Path(REPO_ROOT / "tests").glob("test_*.py"))
        if p.name != Path(__file__).name and needle in p.read_text()
    ]
    assert [p.name for p in patchers] == [
        "test_tool_cache.py",
        "test_upload_names.py",
    ], [p.name for p in patchers]


def test_the_subprocess_strategy_is_hardcoded_into_the_function():
    """Popen, the process group and the timeout are all one body of code.

    A container provider needs the timeout and the group-kill and does not want
    this Popen. Today they cannot be separated, because they are the same
    function.
    """
    source = inspect.getsource(main.run_snakemake)
    assert "subprocess.Popen" in source
    assert "snakemake" in source
    assert "start_new_session=True" in source
    assert "os.killpg" in source
