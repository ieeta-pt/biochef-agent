"""What a client can learn about what a run actually printed (#6).

Recorded before anything changes. Almost nothing.

The runner captures stdout and stderr separately. The handler unpacks both and
throws stdout away -- the variable is literally named `_out` -- then keeps the
last 2000 characters of stderr, and only when the run failed. A run that
succeeded reports nothing at all, and a run that failed reports a tail that may
begin mid-word and may not include the error that mattered.

Nothing attributes any of it to a step. A workflow is a graph of tools; when one
of them fails, the question is always which, and the answer is somewhere in a
truncated string or nowhere.

Snakemake does say. Its output carries `Error in rule <name>:` blocks, and the
emitter derives every rule name from the node id -- dots and dashes become
underscores. So the attribution is available and simply not used.
"""

import inspect
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


def test_stdout_is_thrown_away():
    source = inspect.getsource(main.perform_run)
    assert "code, _out, err = run_snakemake" in source, (
        "stdout is unpacked into a throwaway name and never referred to again"
    )
    assert "_out" not in source.split("run_snakemake")[1].split("return results")[0].replace(
        "code, _out, err = run_snakemake", ""), "stdout is used somewhere after all"


def test_stderr_is_truncated_and_only_on_failure():
    source = inspect.getsource(main.perform_run)
    assert 'err[-2000:]' in source
    assert source.index("if code != 0") < source.index("err[-2000:]"), (
        "the only path that reports stderr is the failure path"
    )


def test_a_successful_run_reports_nothing_it_printed():
    """The outputs come back; what the tools said does not."""
    source = inspect.getsource(main.perform_run)
    collected = source.split("results = {}")[1]
    for evidence in ("err", "_out", "stderr", "stdout"):
        assert evidence not in collected, evidence


def test_there_is_no_logs_endpoint():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/runs/{run_id}" in paths
    assert not any(p and p.endswith("/logs") for p in paths), sorted(
        p for p in paths if p)


def test_nothing_attributes_output_to_a_step():
    """Snakemake names the failing rule; nothing here reads it.

    Confirmed against snakemake 9.21: a rule exiting non-zero produces

        Error in rule broken_step:

    and the emitter builds every rule name from the node id, replacing dots and
    dashes with underscores.
    """
    service = Path(REPO_ROOT / "main.py").read_text()
    assert "Error in rule" not in service
    assert "logs" not in service.lower().replace("dialogs", "")
