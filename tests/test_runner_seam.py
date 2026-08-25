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
import io
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
        """Enough of a Popen for the runner: two streams and a wait().

        The runner reads the pipes itself now rather than calling communicate,
        so a stand-in needs stdout and stderr that end immediately.
        """

        def __init__(self, argv, **kwargs):
            self.pid = os.getpid()      # so os.getpgid() has something real
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            seen["timeout"] = timeout
            return 0

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

        def run(self, ws, timeout_s, on_start=None, on_finish=None,
                on_line=None):
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


def test_a_provider_hands_out_the_process_group_it_created(tmp_path):
    """So something other than the timeout can end a run (#7).

    Cancellation needs precisely the lever the timeout pulls, and the only place
    that knows the group id is the runner. Tested here rather than through the
    cancel endpoint, because those tests stub run_snakemake and call on_start
    themselves -- so removing this from the real runner went unnoticed by all of
    them.

    The id must be the CHILD's own group, not ours: that is what
    start_new_session buys, and killing our own group would take the service
    with it.
    """
    seen = []

    class _Brief(Runner):
        name = "brief-for-test"

        def command(self, ws):
            return ["sh", "-c", "exit 0"]

    taken_back = []

    result = _Brief().run(_Workspace(tmp_path), timeout_s=30,
                          on_start=seen.append,
                          on_finish=lambda: taken_back.append(True))

    assert result.returncode == 0
    assert len(seen) == 1, f"on_start was called {len(seen)} times"
    assert isinstance(seen[0], int)
    assert seen[0] != os.getpgid(0), (
        "the runner handed out this process's own group; killing it would take "
        "the service down with the run"
    )
    assert taken_back == [True], (
        "the group id was handed out and never taken back. It is a number the "
        "kernel reissues once the group is empty, so a caller still holding it "
        "is aiming at whoever gets it next -- and killpg reaches only group "
        "LEADERS, which every run of this service is by construction"
    )


def test_the_group_id_is_taken_back_even_when_the_run_times_out(tmp_path):
    """The timeout path reaps the child too, and must not leave a live number."""
    taken_back = []

    result = _SleepRunner().run(_Workspace(tmp_path), timeout_s=1,
                                on_finish=lambda: taken_back.append(True))

    assert result.returncode == -signal.SIGKILL
    assert taken_back == [True]


class _NeverEnds(Runner):
    """A provider whose command outlives anything that goes wrong around it."""

    name = "never-ends-for-test"

    def command(self, ws):
        return ["sh", "-c", "sleep 120"]


def test_an_unexpected_failure_does_not_leave_the_group_running(tmp_path,
                                                                monkeypatch):
    """The inverse of the stale-pgid bug, and it was in the same finally.

    On the normal and timeout paths the child is reaped before the group id is
    handed back. On any OTHER path -- a broken pipe, an interpreter shutdown,
    a KeyboardInterrupt -- it is not, and handing the id back there left the
    tool running with nothing able to stop it, against a workspace perform_run
    was about to delete.

    The comment on that finally used to say "both paths reach here, and both
    have reaped the child": true of the two it named, false of every other.
    """
    import subprocess

    handed_out = []
    real_wait = subprocess.Popen.wait

    def broken(self, *a, **k):
        raise OSError("the pipe broke")

    # wait(), not communicate(): the runner reads the pipes itself and waits on
    # the process, so this is where an unexpected failure now comes from.
    monkeypatch.setattr(subprocess.Popen, "wait", broken)

    with pytest.raises(OSError):
        _NeverEnds().run(_Workspace(tmp_path), timeout_s=30,
                         on_start=handed_out.append)

    monkeypatch.setattr(subprocess.Popen, "wait", real_wait)

    assert handed_out, "the run never started"
    pgid = handed_out[0]

    # Polled rather than probed once. SIGKILL is asynchronous, and a killed
    # child stays a zombie -- still occupying the group -- until it is reaped,
    # so an immediate check can see a group that is on its way out. Probing
    # once passed on macOS and failed in CI on Linux, which is the sort of
    # difference that makes a test lie about which platform is wrong.
    alive = True
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)

    if alive:                                      # do not leak from the test
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert not alive, (
        f"process group {pgid} is still running after run() failed, and its id "
        f"has already been handed back -- nothing can stop it now"
    )


# --------------------------------------------------------------------------
# streaming, which is what makes per-step progress possible (#8)


class _Chatty(Runner):
    """A provider that writes to both streams and then exits."""

    name = "chatty-for-test"

    def command(self, ws):
        return ["sh", "-c",
                "echo out-one; echo err-one >&2; echo out-two; echo err-two >&2"]


def test_the_runner_reports_each_line_as_it_reads_it(tmp_path):
    """Nothing else drives this.

    The progress tests stub run_snakemake and call on_line themselves, so
    removing the callback from the real runner went unnoticed by every one of
    them -- the same blind spot that hid the pgid hand-out and the hand-back.
    """
    seen = []

    result = _Chatty().run(_Workspace(tmp_path), timeout_s=30,
                           on_line=lambda stream, line: seen.append(
                               (stream, line.strip())))

    assert result.returncode == 0
    assert ("stdout", "out-one") in seen, seen
    assert ("stderr", "err-one") in seen, seen
    assert {stream for stream, _ in seen} == {"stdout", "stderr"}, (
        "both streams must be reported, and each labelled with which it was"
    )


def test_a_failure_in_the_line_callback_does_not_take_the_run_with_it(tmp_path):
    """Reporting is not the job; running the workflow is.

    A defect in whatever is watching the output must not turn a successful run
    into a failed one, and the line must still be collected.
    """
    def explodes(stream, line):
        raise RuntimeError("the watcher is broken")

    result = _Chatty().run(_Workspace(tmp_path), timeout_s=30, on_line=explodes)

    assert result.returncode == 0
    # Every line, not just the first. The reader appends before it reports, so
    # asserting only on line one passed even when the exception killed the
    # reader thread and every later line was lost.
    for expected in ("out-one", "out-two"):
        assert expected in result.stdout, f"{expected} missing: {result.stdout!r}"
    for expected in ("err-one", "err-two"):
        assert expected in result.stderr, f"{expected} missing: {result.stderr!r}"


class _Verbose(Runner):
    """A provider that writes more than fits in a pipe buffer."""

    name = "verbose-for-test"

    def command(self, ws):
        return ["sh", "-c", "i=0; while [ $i -lt 2000 ]; do "
                            "echo \"line-$i\"; echo \"err-$i\" >&2; "
                            "i=$((i+1)); done"]


def test_every_line_is_collected_even_when_there_are_many(tmp_path):
    """Two things at once, and both have bitten real programs.

    Draining one pipe and not the other deadlocks as soon as the undrained one
    fills -- which is the whole reason communicate() exists. And returning
    before the readers have finished loses the tail, because the process can
    exit while its output is still in flight.
    """
    result = _Verbose().run(_Workspace(tmp_path), timeout_s=60)

    assert result.returncode == 0
    assert result.stdout.count("\n") == 2000, (
        f"{result.stdout.count(chr(10))} of 2000 stdout lines survived"
    )
    assert result.stderr.count("\n") == 2000, (
        f"{result.stderr.count(chr(10))} of 2000 stderr lines survived"
    )
    assert "line-1999" in result.stdout, "the tail was lost"
    assert "err-1999" in result.stderr, "the tail was lost"


def test_output_still_in_flight_when_the_process_exits_is_not_lost(tmp_path):
    """The readers are joined before the result is built.

    A process can exit while its output is still being read, and returning at
    that moment loses whatever had not been consumed yet. Ordinarily the readers
    win the race and nothing is missing, which is why dropping the join passed
    every other test here -- so this makes them lose it, by slowing the callback
    down until the process is long gone.
    """
    import time as _time

    class _Burst(Runner):
        name = "burst-for-test"

        def command(self, ws):
            return ["sh", "-c", "i=0; while [ $i -lt 20 ]; do echo \"n-$i\"; "
                                "i=$((i+1)); done"]

    def slowly(stream, line):
        _time.sleep(0.02)

    result = _Burst().run(_Workspace(tmp_path), timeout_s=60, on_line=slowly)

    assert result.returncode == 0
    assert result.stdout.count("\n") == 20, (
        f"only {result.stdout.count(chr(10))} of 20 lines survived; the result "
        f"was built before the readers had finished"
    )
    assert "n-19" in result.stdout, "the tail was lost"
