"""Whether a run can be stopped once it has started (#7).

Recorded before anything changes. It cannot.

CANCELING and CANCELED are in the vocabulary and in the transition table --
QUEUED and INITIALIZING and RUNNING all list CANCELING as a legal successor, and
CANCELING lists CANCELED. Nothing produces either. The state machine describes a
capability the service does not have, which is worse than not describing it: a
client reading the states, or a WES adapter generated from them, would conclude
that cancellation exists.

So a run that is going wrong runs to completion, or until BIOCHEF_RUN_TIMEOUT --
fifteen minutes by default. The only way to stop one sooner is to stop the
service, which stops every other run with it.

The machinery is already there. The runner puts each run in its own process
group precisely so the timeout can kill the group rather than the child; nothing
but the timeout can pull that lever.
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
from runs import ALLOWED, RunState


def test_the_states_exist_and_are_reachable_in_the_table():
    """The vocabulary already promises this."""
    assert RunState.CANCELING in ALLOWED[RunState.RUNNING]
    assert RunState.CANCELING in ALLOWED[RunState.QUEUED]
    assert RunState.CANCELED in ALLOWED[RunState.CANCELING]


def test_but_nothing_in_the_service_ever_produces_them():
    """Which makes the table a description of something that cannot happen."""
    service = Path(REPO_ROOT / "main.py").read_text()
    assert "CANCELING" not in service
    assert "CANCELED" not in service


def test_there_is_no_way_to_ask():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/runs/{run_id}" in paths
    assert not any(p and p.endswith("/cancel") for p in paths), sorted(paths)


def test_the_group_kill_exists_but_only_the_timeout_can_reach_it():
    """The lever is installed and wired to one thing.

    start_new_session puts the run in its own process group so that killpg can
    end the tool and everything it spawned. That is exactly what cancellation
    needs, and only subprocess.TimeoutExpired triggers it.
    """
    source = inspect.getsource(runner_module.Runner.run)
    assert "start_new_session=True" in source
    assert "os.killpg" in source
    assert source.index("except subprocess.TimeoutExpired") < source.index("os.killpg")
    assert "cancel" not in source.lower()
