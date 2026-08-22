"""The seam that lets a workflow be executed some other way (#15).

E2 asks for a second Runner provider. This pins the first one and, more
importantly, pins what a second one gets for free.

The version before this had the timeout, the process-group kill and the choice
of command as one function. A container provider needs the first two and
replaces only the third, and there was no way to say so. Now `run` is written
once on the base class and a provider supplies `command`, so a second provider
cannot quietly lose the group-kill -- which is the part that was hard to get
right and the part whose absence is invisible until a tool is left running.
"""

import inspect
import os
import signal
import time
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

import pytest

import main
from runner import PROVIDERS, Runner, RunResult, SubprocessRunner, get_runner


class _Workspace:
    """Just enough workspace for a runner: a path to work in."""

    def __init__(self, path):
        self.path = str(path)


# --------------------------------------------------------------------------
# the seam exists and is selectable


def test_a_provider_is_chosen_by_name():
    assert isinstance(get_runner("subprocess"), SubprocessRunner)
    assert "subprocess" in PROVIDERS


def test_an_unknown_runner_is_refused_with_the_options_named():
    """At startup, not on the first request.

    A deployment that asked for a runner it does not have should not accept work
    and then fail every submission.
    """
    with pytest.raises(ValueError) as exc:
        get_runner("kubernetes")
    assert "BIOCHEF_RUNNER" in str(exc.value)
    assert "subprocess" in str(exc.value), "the message must say what IS available"


def test_the_service_resolves_a_runner_and_defaults_to_subprocess():
    assert isinstance(main.RUNNER, SubprocessRunner)
    assert "BIOCHEF_RUNNER" in Path(REPO_ROOT / "main.py").read_text()


# --------------------------------------------------------------------------
# behaviour preserved: the subprocess provider launches what it always did


def test_the_subprocess_command_is_unchanged(tmp_path):
    """Pinned exactly, because E2's acceptance is that both runners agree.

    If this argv drifts, "identical outputs" stops being a comparison between
    two ways of running the same thing.
    """
    ws = _Workspace(tmp_path)
    assert SubprocessRunner().command(ws) == [
        "snakemake", "--cores", "4",
        "-s", os.path.join(str(tmp_path), "Snakefile"),
        "-d", str(tmp_path),
    ]


# --------------------------------------------------------------------------
# what a SECOND provider inherits -- the actual point of the seam


class _SleepRunner(Runner):
    """A provider that supplies nothing but a command, as a container one would."""

    name = "sleep-for-test"

    def command(self, ws):
        return ["sh", "-c", "sleep 300"]


def test_a_new_provider_inherits_the_timeout_without_writing_one(tmp_path):
    """Supplying only `command` is enough to get the timeout.

    No readiness race here, unlike the process-group test: whether the child
    starts quickly or slowly, communicate() times out either way and the kill
    still follows.
    """
    result = _SleepRunner().run(_Workspace(tmp_path), timeout_s=2)
    assert result.returncode == -signal.SIGKILL
    assert isinstance(result, RunResult)


def test_no_provider_reimplements_the_kill(tmp_path):
    """The policy lives in one place, so it cannot be lost by a second provider.

    Structural rather than behavioural on purpose: the behaviour is covered for
    the subprocess provider by the process-group test, and it is the same code.
    What this pins is that it stays the same code.
    """
    for name, provider in PROVIDERS.items():
        assert "run" not in vars(provider), (
            f"{name} overrides run(), so it no longer shares the timeout and "
            f"the group-kill with every other provider"
        )
    base = inspect.getsource(Runner.run)
    assert "os.killpg" in base
    assert "start_new_session=True" in base


def test_the_configured_timeout_is_the_one_applied(monkeypatch, tmp_path):
    """Not merely "eventually killed", and not measured by a stopwatch either.

    A regression that doubled the timeout passed the whole suite: the existing
    test waits generously enough that a later kill is still a pass. So a run
    could outlive its configured bound by any factor and nothing would say so,
    which for a service that exists partly to stop a hanging tool is the wrong
    thing to be vague about.

    My first attempt at this asserted on elapsed wall-clock, and a doubled
    timeout slipped under the bound anyway -- any bound loose enough to survive
    a loaded machine is loose enough to hide a factor of two. Recording what is
    actually handed to communicate() is exact and cannot flake.
    """
    import runner as runner_module

    seen = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            self.pid = os.getpid()      # so os.getpgid() has something real
            self.returncode = 0

        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            return ("", "")

    monkeypatch.setattr(runner_module.subprocess, "Popen", _FakePopen)
    _SleepRunner().run(_Workspace(tmp_path), timeout_s=11)

    assert seen["timeout"] == 11, (
        f"the run was bounded at {seen['timeout']!r}, not the configured 11"
    )


class _FailingRunner(Runner):
    """A provider whose command fails, as a broken workflow does."""

    name = "failing-for-test"

    def command(self, ws):
        return ["sh", "-c", "echo to-stderr >&2; exit 3"]


def test_a_failing_run_reports_its_failure(tmp_path):
    """Forcing the success path to return 0 passed the entire suite.

    Nothing exercised a run that fails through a real runner -- the tests that
    cover failure replace run_snakemake wholesale -- so a runner that reported
    every run as successful would have been caught by nothing. The handler
    turns a non-zero code into a 500; reporting 0 for a failed workflow would
    have it collect outputs that were never produced.
    """
    result = _FailingRunner().run(_Workspace(tmp_path), timeout_s=30)

    assert result.returncode == 3, result
    assert "to-stderr" in result.stderr


def test_the_service_runs_through_the_runner_it_resolved(monkeypatch, tmp_path):
    """The wiring, and the mutation that survived everything else.

    Replacing main's `RUNNER.run(...)` with `SubprocessRunner().run(...)` --
    i.e. the service quietly ignoring BIOCHEF_RUNNER and executing every step on
    the host regardless of configuration -- passed the whole suite. Asserting
    that main.RUNNER is the right object says nothing about whether it is used.
    """
    used = {}

    class _Recording(Runner):
        name = "recording"

        def command(self, ws):
            return ["true"]

        def run(self, ws, timeout_s):
            used["ws"] = ws
            used["timeout_s"] = timeout_s
            return RunResult(0, "", "")

    monkeypatch.setattr(main, "RUNNER", _Recording())
    ws = _Workspace(tmp_path)
    result = main.run_snakemake(ws, timeout_s=7)

    assert used.get("ws") is ws, "the configured runner was not the one used"
    assert used.get("timeout_s") == 7
    assert result.returncode == 0


def test_the_command_is_the_only_thing_a_provider_must_supply():
    """A bare provider is an error at the point of use, not a silent no-op."""

    class _Bare(Runner):
        name = "bare"

    with pytest.raises(NotImplementedError):
        _Bare().command(_Workspace("/tmp"))
