"""What the editor could paint on a node while a run is happening (#8).

Recorded before anything changes. Nothing, until the run is over.

B4 wants per-step state -- pending, running, done, failed -- good enough to
colour a node. Two things stand in the way, and only one of them is obvious.

The obvious one: no step has a state. `steps` is populated once, from the
finished stderr, and only ever with the steps that FAILED. A step that is
running, or waiting, or finished successfully, is indistinguishable from a step
that does not exist.

The other one is why the first cannot simply be fixed in the store. The runner
captures output with communicate(), which buffers until the workflow process
exits. So there is nothing to read while the tools are running -- not a little,
none -- and any per-step state derived from that output could only appear after
the entire run had finished, which is exactly when nobody needs it painted.

Snakemake does report as it goes. Confirmed against 9.21, it prints

    localrule step_one:
    Finished jobid: 2 (Rule: step_one)
    1 of 3 steps (33%) done

so the transitions are all there, arriving in real time, into a pipe nobody
reads until the end.
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
import runner as runner_module
from runs import RunStore


def test_a_run_reports_no_per_step_state():
    """Only outputs and an overall state; nothing about individual nodes."""
    store = RunStore()
    run = store.create()
    assert set(run.as_dict()) == {"run_id", "state"}


def test_the_only_per_step_information_is_which_ones_failed():
    """So pending, running and done are all the same thing: absent."""
    import convert
    from steplogs import failing_steps

    # A workflow where one step failed and one succeeded. Only the failure is
    # described; the successful step is absent, which is the same as pending,
    # which is the same as never having existed.
    stderr = "Error in rule step_one:\n    jobid: 1\n"
    attributed = failing_steps(stderr, ["step-one", "step-two"],
                               convert.rule_name_for)

    assert set(attributed) == {"step-one"}
    assert "step-two" not in attributed, (
        "a step that succeeded is indistinguishable from one that never ran"
    )
    assert "status" not in attributed["step-one"], (
        "even the failing step carries no status field, only its error text"
    )


def test_the_output_is_buffered_until_the_process_exits():
    """Which is why no per-step state can exist while a run is happening.

    communicate() returns when the process ends. Nothing reads the pipes before
    that, so the progress snakemake prints as it goes is unavailable until it
    has stopped going.
    """
    source = inspect.getsource(runner_module.Runner.run)
    assert "process.communicate(timeout=timeout_s)" in source
    for streaming in ("readline", "for line in", "Thread"):
        assert streaming not in source, streaming


def test_nothing_reads_the_progress_snakemake_prints():
    """It says "Finished jobid: N (Rule: X)" and "N of M steps"; nobody looks."""
    for module in ("main.py", "runner.py", "steplogs.py"):
        text = Path(REPO_ROOT / module).read_text()
        assert "Finished jobid" not in text, module
        assert "of %d steps" not in text and "steps (" not in text, module
