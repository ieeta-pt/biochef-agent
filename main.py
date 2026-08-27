from convert import *
from convert import rule_name_for
import asyncio
import tempfile
import weakref
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
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
from runs import (IllegalTransition, RunState, RunStore, TERMINAL,
                  UnknownRun)
from datasource import DataSourceError, get_sources
from retention import Retained
from steplogs import Progress, failing_steps
from bodylimit import BodySizeLimitMiddleware, MAX_UPLOAD_BYTES
from runner import SubprocessRunner, get_runner

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
                on_finish=None, on_logs=None, on_progress=None,
                on_outputs=None, retain=None):
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
        snakemake = RUNNER.snakefile_preamble() + convert_to_snakemake(workflow)
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

        report(RunState.RUNNING)

        # Built here because this is where the workflow's nodes are known, and
        # fed from the reader threads as snakemake announces each job. Every
        # node starts PENDING, which is what snakemake implies by saying nothing
        # about a job until it starts it.
        progress = Progress([node.id for node in workflow.nodes], rule_name_for)
        if on_progress is not None:
            on_progress(progress.snapshot())

        def observe(stream, line):
            if progress.observe(line) and on_progress is not None:
                on_progress(progress.snapshot())

        code, out, err = run_snakemake(ws, on_start=on_start,
                                       on_finish=on_finish,
                                       on_line=observe if on_progress else None)

        # Recorded before the failure path raises, because a failed run is
        # exactly the one whose output someone needs. Reporting it only on
        # success, or only as a 2000-character tail, was the whole of #6.
        if on_logs is not None:
            on_logs(out, err, [node.id for node in workflow.nodes])

        if code != 0:
            raise HTTPException(
                status_code=500,
                detail={"error": "execution_failed", "exit_code": code,
                        "stderr_tail": err[-2000:]},
            )

        # Collect results: all data is base64-encoded. Read through the
        # workspace so a tool that replaced its own output with a symlink cannot
        # have the target's contents returned to the client (#41).
        #
        # Whole, and encoded, because that is the response contract the editor
        # speaks. It is also why an output larger than memory cannot come back
        # this way, and why /runs/{id}/outputs/... exists to stream instead.
        results = {}
        catalogue = {}
        for node in workflow.nodes:
            if node.id not in results:
                results[node.id] = {}
                catalogue[node.id] = {}

            for output_name, output in node.outputs.items():
                handle_name = output_name.split("-")[-1]

                with ws.open_read(output.file) as file:
                    raw = file.read()
                    encoded = base64.b64encode(raw).decode("ascii")

                results[node.id][handle_name] = encoded
                # What the streaming endpoint serves: the file inside the
                # workspace, never a path the client supplies.
                catalogue[node.id][handle_name] = output.file

        if on_outputs is not None:
            on_outputs(catalogue)

        return results
    finally:
        # The process was never moved, so there is no global state to restore --
        # only a directory to remove.
        #
        # Kept, if this run's outputs are to remain fetchable. Streaming an
        # output means not having read it into the response, so the file has to
        # still be here when the client asks -- and the alternative, copying
        # everything somewhere on completion, is the same bytes moved twice for
        # no gain. Retention is bounded by time and by count; when it declines,
        # or is switched off, the workspace goes now as it always did.
        if KEEP_WORKSPACE:
            pass
        elif retain is not None and retain(ws):
            pass
        else:
            ws.cleanup()


@app.post("/convert")
async def convert(
    biochef_workflow: str = Form(...),
    files: List[UploadFile] = File(...)
):
    """Unchanged: the whole run happens inside this request.

    Kept as it was because it is the contract the editor speaks today. /runs is
    the same work without the wait.
    """
    # The spooled file itself, not its contents. Starlette has already written
    # anything over a megabyte to disk; reading it back with await f.read()
    # would undo that and put the whole input in memory.
    inputs = [("spooled", f.filename, f.file) for f in files]
    return await run_in_threadpool(perform_run, biochef_workflow, inputs)


MAX_CONCURRENT_RUNS = int(os.getenv("BIOCHEF_MAX_CONCURRENT_RUNS", "4"))
"""How many runs may execute at once.

Without a bound, a burst of submissions became a burst of snakemake processes:
anyio's default thread limiter is 40, so forty tools could be running at once on
a machine sized for rather fewer, each with its own workspace on the same disk.
Accepting work is cheap; doing it is not, and the two need separating.

Runs beyond the limit wait in QUEUED, which is what that state is for -- WES
means "accepted, not yet started" by it, and a client polling sees exactly that.
"""

_slots_by_loop = weakref.WeakKeyDictionary()


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
    """
    loop = asyncio.get_running_loop()
    semaphore = _slots_by_loop.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        _slots_by_loop[loop] = semaphore
    return semaphore

_running = set()
"""Strong references to the tasks in flight.

asyncio.create_task returns a task the caller is expected to keep. The event
loop holds only a WEAK reference, so a task nobody else refers to can be
garbage collected part-way through -- documented CPython behaviour, and a
particularly unpleasant one here, because the run would simply stop, stay
non-terminal, and be polled forever by a client waiting for an answer that is
never coming.
"""

RETAINED = Retained()
"""Workspaces kept so their outputs can be streamed.

Bounded by time and by count, because "stop deleting" is how a service fills a
disk. Disabled by setting either bound to zero, which restores the old behaviour
of removing a workspace the moment its run ends.
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
        RUNS.attach(run_id, pgid)

    def finished():
        RUNS.detach(run_id)

    def step_progress(step_status):
        # Named apart from `progress` above, which reports RUN state. An earlier
        # version called both of them progress, so the second definition
        # shadowed the first and every state transition was handed to
        # record_progress as if it were a per-step map.
        RUNS.record_progress(run_id, step_status)

    def outputs(catalogue):
        RUNS.record_outputs(run_id, catalogue)

    def retain(ws):
        return RETAINED.keep(run_id, ws)

    def logs(stdout, stderr, node_ids):
        # Attributed here, where the emitter is already imported, so the run
        # store does not have to reach for it.
        steps = failing_steps(stderr, node_ids, rule_name_for)
        RUNS.record_logs(run_id, stdout, stderr, steps)

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
                finished, logs, step_progress, outputs, retain)
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

    A run may have reached a terminal state already -- cancelled, once #7
    exists -- and the worker will not know. Refusing loudly inside a background
    task would only raise into nowhere.
    """
    try:
        RUNS.advance(run_id, state, **detail)
    except (IllegalTransition, UnknownRun):
        pass


@app.post("/runs", status_code=202)
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
    # Read to disk here rather than kept as the request's own spooled file. The
    # request is over long before the work starts, and starlette closes what it
    # spooled when it ends -- so the asynchronous path has to take ownership of
    # the bytes while it still can, without holding them in memory.
    inputs = []
    for f in files:
        spooled = tempfile.NamedTemporaryFile(delete=False)
        try:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                spooled.write(chunk)
        finally:
            spooled.close()
        inputs.append(("handedover", f.filename, spooled.name))

    run = RUNS.create()
    task = asyncio.create_task(_execute(run.run_id, biochef_workflow, inputs))
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
    group, and that group is ended -- the tool and everything it spawned,
    exactly as the timeout does it, because a tool that spawns children and
    survives its parent is the reason group-killing is there at all.

    The reply is CANCELING rather than CANCELED, and that is not evasion: the
    kill has been issued, but the run is not over until the worker has finished
    tidying up and said so. Reporting CANCELED here would be claiming something
    that has not happened yet.
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


@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str):
    """What the run printed, and which step snakemake blamed.

    Two halves, and the response says which is which rather than blurring them.
    `steps` names the nodes that FAILED, because snakemake states that outright.
    A step that succeeded is not separated out: its output is in `stdout` along
    with everything else's, and nothing marks where one rule's writing ends.
    Splitting that needs a log: directive per rule, which is the emitter's
    business.

    Available while a run is still going, not only at the end -- there is no
    reason to make someone wait for a fifteen-minute run to finish before seeing
    what it has said so far.
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


@app.get("/runs/{run_id}/outputs/{node_id}/{handle}")
async def stream_output(run_id: str, node_id: str, handle: str):
    """One output, streamed, without base64 and without holding it in memory.

    The endpoint D2 needs. /convert and /runs still return everything encoded in
    one response, because that is the contract the editor speaks -- but an
    output larger than memory cannot come back that way, and this is how it
    comes back instead.

    A client names a node and a handle, never a path. What those mean is read
    from what the run recorded, so nothing a caller sends decides which file is
    opened, and the file is opened through the workspace -- so a tool that
    replaced its own output with a symlink still cannot have the target's
    contents served (#41).
    """
    try:
        run = RUNS.get(run_id)
    except UnknownRun:
        raise HTTPException(
            status_code=404,
            detail=f"no run {run_id!r}; it never existed, or it finished long "
                   f"enough ago to have been forgotten",
        )

    filename = (run.output_files.get(node_id) or {}).get(handle)
    if filename is None:
        raise HTTPException(
            status_code=404,
            detail=f"run {run_id!r} has no output {handle!r} on node "
                   f"{node_id!r}",
        )

    ws = RETAINED.workspace(run_id)
    if ws is None:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "outputs_expired",
                "run_id": run_id,
                "message": "this run's outputs are no longer on disk. They are "
                           "kept for BIOCHEF_KEEP_OUTPUTS seconds and for the "
                           "most recent BIOCHEF_MAX_RETAINED_RUNS runs.",
            },
        )

    try:
        handle_in = ws.open_read(filename)
    except (FileNotFoundError, UnsafeName) as failure:
        raise HTTPException(
            status_code=404,
            detail=f"output {handle!r} of {node_id!r} is not there: {failure}",
        )

    def chunks():
        # Closed by the generator rather than a context manager, because the
        # response outlives this function -- starlette iterates it after the
        # handler has returned.
        try:
            while True:
                chunk = handle_in.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            handle_in.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f'attachment; filename="{node_id}-{handle}"'},
    )
