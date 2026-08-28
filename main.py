from convert import *
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

import profiles
from workspace import UnsafeName, check_name, make_workspace
from auth import AuthenticationMiddleware, NoAuth, get_auth
from bodylimit import BodySizeLimitMiddleware, MAX_UPLOAD_BYTES
from runner import SubprocessRunner, get_runner

# The service is starting, so say what it is configured to permit. convert
# applied the profile at import, because every setting here is read at import;
# this is the first point at which saying so is not a side effect of somebody
# importing a module.
print(profiles.describe(PROFILE))

app = FastAPI()

# Before anything reads the body. starlette spools the whole multipart payload
# before the handler is entered, so a limit enforced in /convert would be
# refusing bytes that are already on disk (#11).
app.add_middleware(BodySizeLimitMiddleware)

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


def run_snakemake(ws, timeout_s=RUN_TIMEOUT_S):
    """Execute the workflow with the configured runner.

    Kept as a function, rather than calling RUNNER.run at the call site, so that
    the timeout default lives in one place and the handler does not have to know
    which provider it got.
    """
    return RUNNER.run(ws, timeout_s)


class BiochefWorkflow(BaseModel):
    nodes: list
    edges: list


@app.post("/convert")
async def convert(
    biochef_workflow: str = Form(...),
    files: List[UploadFile] = File(...)
):
    ws = make_workspace(RUN_ROOT)
    try:
        # Parse workflow
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
        for f in files:
            name = check_name(f.filename)
            if name not in expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"upload {name!r} is not an input of this workflow; "
                           f"it expects {sorted(expected)}",
                )
            try:
                ws.write_bytes(name, await f.read())
            except FileExistsError:
                raise HTTPException(
                    status_code=400,
                    detail=f"upload {name!r} was sent twice, or shadows a "
                           f"file this run already created",
                )
            seen.add(name)

        if expected - seen:
            raise HTTPException(
                status_code=400,
                detail=f"missing inputs: {sorted(expected - seen)}",
            )

        # Convert workflow to Snakemake and run
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

        # Off the event loop: communicate() blocks, and running it inline would
        # mean the service never has two runs in flight to keep apart.
        code, _out, err = await run_in_threadpool(run_snakemake, ws)
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
        if not KEEP_WORKSPACE:
            ws.cleanup()
