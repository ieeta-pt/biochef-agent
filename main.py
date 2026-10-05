from convert import *
from convert import rule_name_for
import asyncio
import contextlib
import weakref
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from typing import List
import json
from pydantic import BaseModel
import os
import signal
import subprocess
import base64

from workspace import UnsafeName, check_name, make_workspace
from auth import AuthenticationMiddleware, NoAuth, get_auth
from runs import (IllegalTransition, MAX_RUNS, RunCapacityError, RunState,
                  RunStore, TERMINAL, UnknownRun)
from datasource import DataSourceError, get_sources
from steplogs import (Progress, clamp, failing_steps, make_step_log_names,
                      read_node_logs)
from bodylimit import BodySizeLimitMiddleware, MAX_UPLOAD_BYTES
from evidence_verification import EvidenceVerificationError
from runner import SubprocessRunner, get_runner
from signing import SignatureError

app = FastAPI()

# Before anything reads the body. starlette spools the whole multipart payload
# before the handler is entered, so a limit enforced in /convert would be
# refusing bytes that are already on disk (#11).
app.add_middleware(BodySizeLimitMiddleware)

SOURCES = get_sources()
"""Where this deployment permits inputs to come from.

Resolved at import, like the runner and the auth provider, so a deployment
naming a source that does not exist fails to start rather than refusing every
submission that uses it.
"""

AUTH = get_auth(os.getenv("BIOCHEF_AUTH", NoAuth.name))
"""Who may ask this service to run something.

Resolved at import so a deployment naming a provider it does not have, or asking
for bearer without a token, fails to start rather than accepting work.
"""

# Added last, so it is OUTERMOST and runs before the body limit -- and therefore
# before any of the body is accepted. An anonymous caller should not be able to
# make this service buffer half a gigabyte before being told no (#10).
app.add_middleware(AuthenticationMiddleware, provider=AUTH)


@app.exception_handler(UnsafeName)
async def unusable_name(request, exc):
    """A bad name is the client's mistake, so say so rather than returning 500."""
    return JSONResponse(status_code=400, content={"detail": f"unusable file name: {exc}"})


@app.exception_handler(ToolIntegrityError)
async def tool_integrity(request, exc):
    """502, because the failure is upstream and not the client's doing.

    Unhandled, this surfaced as a bare "Internal Server Error" -- accurate about
    nothing. Nothing was leaked, but nothing was said either, and an operator
    reading a 500 has no reason to look at the registry.

    The detail is the exception's own message, which names the artifact and the
    two digests and says explicitly that a tag moving mid-pull looks the same
    from here. It carries no local paths.
    """
    return JSONResponse(
        status_code=502,
        content={"detail": {"error": "tool_integrity", "message": str(exc)}},
    )


@app.exception_handler(SignatureError)
@app.exception_handler(EvidenceVerificationError)
async def artifact_verification(request, exc):
    """Refuse an artifact that does not satisfy the local execution policy."""
    return JSONResponse(
        status_code=403,
        content={
            "detail": {
                "error": "artifact_verification",
                "message": str(exc),
            }
        },
    )


RUN_ROOT = os.getenv("BIOCHEF_RUN_ROOT") or None
RUN_TIMEOUT_S = int(os.getenv("BIOCHEF_RUN_TIMEOUT", "900"))
KEEP_WORKSPACE = os.getenv("BIOCHEF_KEEP_WORKSPACE", "false").lower() == "true"


RUNNER = get_runner(os.getenv("BIOCHEF_RUNNER", SubprocessRunner.name))
"""How this deployment executes a workflow.

Resolved at import so a deployment that names a runner it does not have fails to
start, rather than accepting work and failing every submission.
"""


def run_snakemake(ws, timeout_s=RUN_TIMEOUT_S, on_start=None, on_finish=None,
                  on_line=None):
    """Execute the workflow with the configured runner.

    Kept as a function, rather than calling RUNNER.run at the call site, so that
    the timeout default lives in one place and the handler does not have to know
    which provider it got.
    """
    return RUNNER.run(ws, timeout_s, on_start=on_start, on_finish=on_finish,
                      on_line=on_line)


class BiochefWorkflow(BaseModel):
    nodes: list
    edges: list


def perform_run(biochef_workflow: str, inputs, progress=None, on_start=None,
                on_finish=None, on_logs=None, cancel_requested=None,
                on_progress=None):
    """One run, start to finish, given its inputs already resolved.

    Split out of the handler so the synchronous endpoint and the asynchronous one
    execute the same code rather than two copies that drift.

    `inputs` is a list of (source, name, spec). The spec is whatever that source
    needs -- bytes for an upload, a path for localpath -- and uploads arrive
    already read because the asynchronous path has to consume them while the
    request is still open; by the time the work runs there is no request left to
    read from.

    Synchronous on purpose: every step here blocks, and both callers hand it to a
    worker thread. `progress` is called with each RunState as it is entered, and
    is None for the synchronous path, which has nowhere to report it.
    """
    def report(state):
        if progress is not None:
            progress(state)

    report(RunState.INITIALIZING)
    ws = make_workspace(RUN_ROOT)
    try:
        workflow_dict = json.loads(biochef_workflow)
        workflow = parse_biochef_workflow(workflow_dict)

        # The tools go in first, so that an upload named after a binary is
        # refused by O_EXCL rather than quietly replacing what will be executed.
        materialise_tools(workflow, ws)

        # Save uploaded files, against the set the workflow says it needs.
        #
        # The name is checked for shape -- starlette passes the multipart
        # filename through verbatim -- and then for whether this run has any
        # business receiving it. The second gate is what stops an upload
        # occupying a slot the run means to produce: snakemake sees the output
        # already present and up to date, skips the rule that would have made
        # it, and the client's bytes are returned as that tool's output. The
        # tool never ran, and nothing in the response says so.
        #
        # O_EXCL cannot catch that on its own, because at upload time the
        # output does not exist yet.
        expected = expected_uploads(workflow)
        seen = set()
        for source_name, filename, spec in inputs:
            name = check_name(filename)
            if name not in expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"input {name!r} is not an input of this workflow; "
                           f"it expects {sorted(expected)}",
                )
            # Which names are legitimate is settled here, against the workflow,
            # before any provider is asked. A source deciding its own
            # destination is how a fetch becomes a write to somewhere else.
            source = SOURCES.get(source_name)
            if source is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"input {name!r} names source {source_name!r}, which "
                           f"this deployment does not permit; it allows "
                           f"{sorted(SOURCES)}",
                )
            try:
                source.fetch(ws, name, spec)
            except FileExistsError:
                raise HTTPException(
                    status_code=400,
                    detail=f"input {name!r} was supplied twice, or shadows a "
                           f"file this run already created",
                )
            except DataSourceError as failure:
                raise HTTPException(status_code=400, detail=str(failure))
            seen.add(name)

        if expected - seen:
            raise HTTPException(
                status_code=400,
                detail=f"missing inputs: {sorted(expected - seen)}",
            )

        # The runner may need lines of its own at the top -- a container
        # directive, for the provider that runs each step in one. Asking the
        # runner keeps the emitter from having to know how the workflow will be
        # executed.
        step_logs = (make_step_log_names(len(workflow.nodes))
                     if on_logs is not None else None)
        snakemake = RUNNER.snakefile_preamble() + convert_to_snakemake(
            workflow, step_logs=step_logs)
        # Same mapping as the upload loop. An upload named "Snakefile" -- or,
        # on a case-insensitive filesystem, "SNAKEFILE" -- occupies this slot
        # first, and O_EXCL then refuses the generated write. That is the right
        # refusal, but without this it surfaced as an unhandled 500 for what is
        # a bad request.
        try:
            ws.write_bytes("Snakefile", snakemake.encode())
        except FileExistsError:
            raise HTTPException(
                status_code=400,
                detail="an upload occupies a name this run needs: 'Snakefile'",
            )

        if cancel_requested is not None and cancel_requested():
            return None

        report(RunState.RUNNING)

        # Built here because this is where the workflow's nodes are known, and
        # fed from the reader threads as snakemake announces each job. Every
        # node starts PENDING, which is what snakemake implies by saying nothing
        # about a job until it starts it.
        tracker = Progress([node.id for node in workflow.nodes], rule_name_for)
        if on_progress is not None:
            on_progress(tracker.snapshot())

        def observe(stream, line):
            if stream == "stderr" and tracker.observe(line) and on_progress is not None:
                on_progress(tracker.snapshot())

        code, out, err = run_snakemake(ws, on_start=on_start,
                                       on_finish=on_finish,
                                       on_line=observe if on_progress is not None else None)

        # Preserve logs even when the runner exits nonzero, before reporting
        # the execution failure.
        if on_logs is not None:
            node_ids = [node.id for node in workflow.nodes]
            on_logs(code, out, err, node_ids,
                    read_node_logs(ws, node_ids, step_logs))

        if code != 0:
            raise HTTPException(
                status_code=500,
                detail={"error": "execution_failed", "exit_code": code,
                        "stderr_tail": err[-2000:]},
            )

        # Collect results: all data is base64-encoded. Read through the
        # workspace so a tool that replaced its own output with a symlink cannot
        # have the target's contents returned to the client (#41).
        results = {}
        for node in workflow.nodes:
            if node.id not in results:
                results[node.id] = {}

            for output_name, output in node.outputs.items():
                handle_name = output_name.split("-")[-1]

                with ws.open_read(output.file) as file:
                    raw = file.read()
                    encoded = base64.b64encode(raw).decode("ascii")

                results[node.id][handle_name] = encoded

        return results
    finally:
        # The process was never moved, so there is no global state to restore --
        # only a directory to remove, and it goes whether the run succeeded or
        # not.
        if KEEP_WORKSPACE:
            ws.close()
        else:
            ws.cleanup()


@app.post("/convert")
async def convert(
    biochef_workflow: str = Form(...),
    files: List[UploadFile] = File(...)
):
    """The editor's synchronous contract, sharing the execution limit.

    Kept as it was because it is the contract the editor speaks today. /runs is
    the same work without the wait.
    """
    async with _slots():
        inputs = [("upload", f.filename, await f.read()) for f in files]
        return await run_in_threadpool(perform_run, biochef_workflow, inputs)


MAX_CONCURRENT_RUNS = int(os.getenv("BIOCHEF_MAX_CONCURRENT_RUNS", "4"))
"""How many /runs and /convert workflows may execute at once.

Without a bound, a burst of submissions became a burst of snakemake processes:
anyio's default thread limiter is 40, so forty tools could be running at once on
a machine sized for rather fewer, each with its own workspace on the same disk.
Accepting work is cheap; doing it is not, and the two need separating.

Runs beyond the limit wait in QUEUED, which is what that state is for -- WES
means "accepted, not yet started" by it, and a client polling sees exactly that.
"""
if MAX_CONCURRENT_RUNS < 1:
    raise ValueError("BIOCHEF_MAX_CONCURRENT_RUNS must be positive")

_slots_by_loop = weakref.WeakKeyDictionary()
_occupied_by_loop = weakref.WeakKeyDictionary()


def _slots():
    """The semaphore for whichever event loop is running.

    Not one module-level Semaphore. asyncio locks bind themselves to a loop the
    first time a waiter is created -- so a single shared one works until it is
    contended, and from then on any use from a different loop raises
    "is bound to a different event loop".

      loop 1 with contention: ok
      loop 2 with contention: RuntimeError: <Semaphore [locked]> is bound to a
                              different event loop

    A server runs one loop, so this would not have bitten in production. It
    would have bitten in tests, which build a fresh loop per TestClient -- and
    only did not because the test that forces contention substitutes its own
    semaphore. A bound that breaks the moment someone tests it properly is not
    much of a bound.

    Weakly keyed, so a finished loop takes its semaphore with it.

    It also counts how many slots are held, because /capacity has to report
    that and a Semaphore's own count is private. Counting run states instead
    would be wrong: /convert holds a slot for the whole of a synchronous
    conversion without ever creating a run record, so an agent with every slot
    busy converting would report itself entirely free -- which is the exact
    mistake /capacity exists to stop a hub making.

    Decremented in a finally, so a cancelled or failed request gives its slot
    back in the count as well as in the semaphore. The two are released
    together or the number drifts until a restart.
    """
    loop = asyncio.get_running_loop()
    semaphore = _slots_by_loop.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        _slots_by_loop[loop] = semaphore
    return _holding(loop, semaphore)


@contextlib.asynccontextmanager
async def _holding(loop, semaphore):
    """Hold a slot, and be counted while holding it."""
    async with semaphore:
        _occupied_by_loop[loop] = _occupied_by_loop.get(loop, 0) + 1
        try:
            yield
        finally:
            _occupied_by_loop[loop] = _occupied_by_loop.get(loop, 1) - 1


def slots_busy() -> int:
    """Slots held right now on the running loop, by either route.

    Clamped, because a number outside the bounds would be reported as fact. If
    the count ever drifts, a wrong free count is the one thing this endpoint
    must not produce.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return max(0, min(MAX_CONCURRENT_RUNS, _occupied_by_loop.get(loop, 0)))

_running = set()
"""Strong references to the tasks in flight.

asyncio.create_task returns a task the caller is expected to keep. The event
loop holds only a WEAK reference, so a task nobody else refers to can be
garbage collected part-way through -- documented CPython behaviour, and a
particularly unpleasant one here, because the run would simply stop, stay
non-terminal, and be polled forever by a client waiting for an answer that is
never coming.
"""

RUNS = RunStore()
"""Runs this process is aware of.

In memory, so nothing survives a restart and nothing is shared between replicas.
Both are real limits rather than oversights, and both are why a persistent store
is its own piece of work.
"""


async def _execute(run_id: str, biochef_workflow: str, inputs):
    """Do the run, and record how it ended.

    Every path out of here reaches a terminal state. A run stuck in RUNNING
    because something raised on the way to recording a failure would be worse
    than a run that failed: a client polling it would wait forever.
    """
    def progress(state):
        _advance(run_id, state)

    def started(pgid):
        if RUNS.attach(run_id, pgid):
            # Cancellation may have arrived while the runner was starting.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def finished():
        RUNS.detach(run_id)

    def step_progress(step_status):
        # Named apart from `progress` above, which reports RUN state. An earlier
        # version called both of them progress, so the second definition
        # shadowed the first and every state transition was handed to
        # record_progress as if it were a per-step map.
        RUNS.record_progress(run_id, step_status)

    def logs(exit_code, stdout, stderr, node_ids, node_logs):
        # Attributed here, where the emitter is already imported, so the run
        # store does not have to reach for it.
        # A successful tool can print a Snakemake-looking error heading. Only
        # parse a failed workflow, and only from the retained stderr tail.
        steps = (failing_steps(clamp(stderr), node_ids, rule_name_for)
                 if exit_code != 0 else {})
        RUNS.record_logs(run_id, stdout, stderr, steps, node_logs)

    try:
        # Waits here while the service is busy, and the run stays QUEUED until a
        # slot frees. Acquiring before anything else means a queued run has not
        # yet made a workspace or pulled a tool.
        async with _slots():
            # Asked for while queued, and never started. Nothing was executed,
            # so there is nothing to kill -- only a state to settle.
            if RUNS.get(run_id).state is RunState.CANCELING:
                _advance(run_id, RunState.CANCELED)
                return
            results = await run_in_threadpool(
                perform_run, biochef_workflow, inputs, progress, started,
                finished, logs, cancel_requested=lambda: _was_cancelled(run_id),
                on_progress=step_progress)
    except HTTPException as refusal:
        if _was_cancelled(run_id):
            _advance(run_id, RunState.CANCELED)
            return
        # The run failed for a reason attributable to what was submitted or to
        # the tools it named -- a bad workflow, a missing input, a tool exiting
        # non-zero. WES calls that EXECUTOR_ERROR.
        _advance(run_id, RunState.EXECUTOR_ERROR, error=refusal.detail)
    except Exception as failure:                     # noqa: BLE001
        if _was_cancelled(run_id):
            _advance(run_id, RunState.CANCELED)
            return
        # Anything else is us, not the submission. SYSTEM_ERROR says so rather
        # than blaming the workflow for a defect in this service.
        _advance(run_id, RunState.SYSTEM_ERROR,
                 error={"error": "system_error", "message": str(failure)})
    else:
        if _was_cancelled(run_id):
            # The kill lost the race and the work finished anyway. It was still
            # asked to stop, and saying COMPLETE would hand back outputs the
            # caller has said they do not want.
            _advance(run_id, RunState.CANCELED)
            return
        _advance(run_id, RunState.COMPLETE, outputs=results)


def _was_cancelled(run_id) -> bool:
    try:
        return RUNS.get(run_id).state is RunState.CANCELING
    except UnknownRun:
        return False


def _advance(run_id, state, **detail):
    """Record a transition, tolerating one that is no longer legal.

    A cancellation can arrive after the worker checks the state but before it
    records a terminal result. In that case the worker must settle CANCELING,
    not leave it there forever.
    """
    try:
        RUNS.advance(run_id, state, **detail)
    except IllegalTransition:
        if state in (RunState.COMPLETE, RunState.EXECUTOR_ERROR,
                     RunState.SYSTEM_ERROR) and _was_cancelled(run_id):
            try:
                RUNS.advance(run_id, RunState.CANCELED)
            except (IllegalTransition, UnknownRun):
                pass
    except UnknownRun:
        pass


@app.post("/runs", status_code=202, responses={
    503: {"description": "Agent capacity reached; retry later"},
})
async def submit_run(
    biochef_workflow: str = Form(...),
    files: List[UploadFile] = File(...)
):
    """Accept a workflow and answer immediately with something to poll.

    The uploads are read here, while the request is still open. By the time the
    work runs there is no request left to read them from -- which is the whole
    difference between this and /convert, and the reason it cannot simply call
    the same handler in the background.
    """
    try:
        run = RUNS.create()
    except RunCapacityError:
        raise HTTPException(
            status_code=503, detail="agent run capacity reached; retry later",
            headers={"Retry-After": "1"},
        ) from None
    try:
        inputs = [("upload", f.filename, await f.read()) for f in files]
        task = asyncio.create_task(_execute(run.run_id, biochef_workflow, inputs))
    except BaseException:
        RUNS.discard(run.run_id)
        raise
    # Held until it finishes, then dropped. See _running above: without this the
    # task can be collected mid-run and the run never reaches a terminal state.
    _running.add(task)
    task.add_done_callback(_running.discard)
    return run.as_dict()


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Where a run has got to, and its outputs once it is COMPLETE."""
    try:
        return RUNS.get(run_id).as_dict()
    except UnknownRun:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r}; it never existed, or it finished long "
                   f"enough ago to have been forgotten",
        )


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Ask a run to stop, and end the processes doing it.

    The path is WES's, so exposing this as a WES endpoint later (F5) does not
    move it.

    Two shapes of run, and they differ. One waiting for a slot has executed
    nothing, so cancelling it is a matter of state: it settles CANCELED when its
    turn comes and it declines to start. One that is running has a process
    group, and that group is ended -- the tool and children that remain in the
    group, exactly as the timeout does it.

    The reply is normally CANCELING while the worker tidies up. It may already
    be CANCELED if the worker finishes before the response; CANCELED is only
    recorded after cleanup.
    """
    try:
        run = RUNS.get(run_id)
    except UnknownRun:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r}; it never existed, or it finished long "
                   f"enough ago to have been forgotten",
        )

    if run.state in TERMINAL:
        raise HTTPException(
            status_code=409,
            detail={"error": "already_finished", "run_id": run_id,
                    "state": run.state.value,
                    "message": "this run has already ended; there is nothing "
                               "to cancel"},
        )

    try:
        RUNS.advance(run_id, RunState.CANCELING)
    except IllegalTransition:
        # Someone else asked first, or it ended between the check and here.
        return RUNS.get(run_id).as_dict()

    pgid = run.pgid
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            # Already gone -- it finished on its own in the meantime. The
            # worker will settle the state.
            pass

    return RUNS.get(run_id).as_dict()


AGENT_VERSION = os.getenv("BIOCHEF_AGENT_VERSION", "").strip()
"""What this deployment calls itself, for a hub that talks to several.

Empty by default and reported as null rather than invented. A version this
service made up would be worse than none, because a hub routing work would
believe it and act on it. A deployment that wants one sets it, usually to the
commit it was built from.

Stripped, because a value that is only whitespace is a deployment that did not
set one. A CI template substituting an empty variable produces exactly that,
and "   " reported as a version is believed as readily as a real one.
"""

# The states that occupy an execution slot. QUEUED is admitted and waiting, so
# counting it as busy would have a hub route away from an agent that is in fact
# free; CANCELING still holds the slot until the worker lets go of it.
BUSY_STATES = (RunState.INITIALIZING, RunState.RUNNING, RunState.CANCELING)


@app.get("/health")
async def health():
    """Up. Nothing else.

    Answers without credentials -- see AuthenticationMiddleware.OPEN -- because
    the thing that probes it is an orchestrator and not a person, and has none
    to offer. An endpoint that demanded a token would turn a mistyped token into
    a healthy service that looks dead and is restarted forever.

    Which is exactly why it says nothing else. Capacity, runner and provider all
    describe this deployment, they live behind authentication in /capacity, and
    putting any of them here would publish them to whatever can reach the port.
    """
    return {"status": "ok"}


@app.head("/health")
async def health_probe():
    """The same answer to a HEAD probe, which several checkers send by default.

    Declared separately rather than as api_route(methods=["GET", "HEAD"]):
    fastapi derives one operation id per route, so a two-method route puts the
    same id on both operations -- and labelled the GET one `..._head` -- which
    collides in any generated client. The contract is exported and checked, so
    that lands in openapi.json.

    fastapi's APIRoute, unlike starlette's Route, does not add HEAD to a GET
    route on its own; without this a HEAD probe gets 405 from a service that is
    up, and 405 reads as unhealthy. A HEAD carries no body back, so this
    withholds nothing the GET does not already say.
    """
    return {"status": "ok"}


@app.get("/capacity")
async def capacity():
    """What this agent is, and how much of it is free.

    For a hub deciding where to send work. Authenticated, because every field
    here describes the deployment rather than merely whether it is up.

    Today the only way to discover saturation is to submit and be refused with
    503, which is finding out after the work went to the wrong site.

    Occupancy is read from the slot semaphore and not from run states, because
    /convert holds a slot for a whole synchronous conversion without creating a
    run record. Counting run states would report an agent with every slot busy
    converting as entirely free. `runs.in_flight` is the asynchronous subset,
    so the gap between it and `slots.busy` is exactly the /convert calls in
    progress.
    """
    counts = RUNS.state_counts()
    busy = slots_busy()
    return {
        "version": AGENT_VERSION or None,
        # The provider the middleware actually enforces with, not a second
        # reading of the environment. A field that says `bearer` while the stack
        # admits anyone is worse than no field.
        "authentication": AUTH.name,
        "runner": RUNNER.name,
        # What a hub routes on. Both routes draw on these.
        "slots": {
            "total": MAX_CONCURRENT_RUNS,
            "busy": busy,
            "free": MAX_CONCURRENT_RUNS - busy,
        },
        "runs": {
            # Runs occupying a slot: INITIALIZING, RUNNING, CANCELING. Not the
            # same as slots.busy, which also counts synchronous /convert.
            "in_flight": sum(counts.get(state.value, 0)
                             for state in BUSY_STATES),
            # Admitted and waiting for a slot, so not counted as busy: an agent
            # with a queue still has free slots the moment one is released, and
            # a hub told otherwise would route away from a site that is free.
            "queued": counts.get(RunState.QUEUED.value, 0),
            # Whether a submission would be admitted at all, which free slots
            # do not answer: a full set of non-terminal runs refuses with 503
            # even with every slot idle. A hub told only about slots routes
            # work to an agent that will refuse it.
            "accepting": RUNS.accepting(),
            "by_state": counts,
        },
        "retained": {"runs": RUNS.retained(), "cap": MAX_RUNS},
        # Null, not []. The DataSource interface is now in this tree, but
        # neither provider it ships with can enumerate what a site holds: an
        # upload source has nothing until a caller sends it, and a localpath
        # source has a root directory rather than a catalogue. So this build
        # still cannot answer, and [] would read as "this site holds none".
        # A provider that can enumerate -- DRS (#80) -- is what fills this in.
        "datasets": None,
    }


@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str):
    """Run-level output and separately captured output from each executed node.

    `stdout` and `stderr` are Snakemake's run-wide streams. `node_logs` has
    stdout and stderr from separate files for each rule that started, including
    a failing tool's actual stderr. Stdout already directed into a declared
    scientific output remains in that output and is not copied into a log.
    `failed_steps` is a diagnostic index of Snakemake-style error headings in the
    run-wide stderr; tools can imitate those headings, so it is not proof of
    failure or a substitute for `node_logs`.

    Logs are recorded after the workflow process exits, before output collection
    and the run's terminal state. They are not available during execution.
    Each stream's retained per-node tails share BIOCHEF_MAX_LOG_BYTES across
    nodes; log files on disk can grow until the run ends.
    """
    try:
        run = RUNS.get(run_id)
    except UnknownRun:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r}; it never existed, or it finished long "
                   f"enough ago to have been forgotten",
        )
    return run.logs_as_dict()
