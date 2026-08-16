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

from workspace import UnsafeName, check_name, make_workspace

app = FastAPI()


@app.exception_handler(UnsafeName)
async def unusable_name(request, exc):
    """A bad name is the client's mistake, so say so rather than returning 500."""
    return JSONResponse(status_code=400, content={"detail": f"unusable file name: {exc}"})


RUN_ROOT = os.getenv("BIOCHEF_RUN_ROOT") or None
RUN_TIMEOUT_S = int(os.getenv("BIOCHEF_RUN_TIMEOUT", "900"))
KEEP_WORKSPACE = os.getenv("BIOCHEF_KEEP_WORKSPACE", "false").lower() == "true"


def run_snakemake(ws, timeout_s=RUN_TIMEOUT_S):
    """Run the workflow in its own directory, without moving this process.

    -s and -d give snakemake the Snakefile and the working directory
    explicitly, which is what makes a per-run directory possible: relative paths
    in the rules resolve under -d, and the shell blocks run with that as their
    cwd, so the emitter's ./{bin} convention is unchanged.

    start_new_session puts snakemake and everything it spawns in one process
    group, and the timeout kills the GROUP. Killing only the child leaves the
    tool running and then blocks forever on the pipes the orphan still holds --
    measured at 7s and counting, against 2s for the group. The pgid is captured
    before the first wait, because after the child is reaped getpgid raises.
    """
    process = subprocess.Popen(
        ["snakemake", "--cores", "4", "-s", os.path.join(ws.path, "Snakefile"),
         "-d", ws.path],
        cwd=ws.path, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    pgid = os.getpgid(process.pid)
    try:
        out, err = process.communicate(timeout=timeout_s)
        return process.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(pgid, signal.SIGKILL)
        out, err = process.communicate()
        return -signal.SIGKILL, out or "", err or ""


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

        # Save uploaded files. The name is checked rather than trusted:
        # starlette passes the multipart filename through verbatim. The write
        # goes through the workspace's directory descriptor, so even a name that
        # slipped past the check could not land outside it.
        for f in files:
            try:
                ws.write_bytes(check_name(f.filename), await f.read())
            except FileExistsError:
                raise HTTPException(
                    status_code=400,
                    detail=f"upload {f.filename!r} was sent twice, or shadows a "
                           f"file this run already created",
                )

        # Convert workflow to Snakemake and run
        snakemake = convert_to_snakemake(workflow)
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
