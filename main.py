from convert import *
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import List
import json
from pydantic import BaseModel
import os
import base64

from workspace import UnsafeName, check_name

app = FastAPI()


@app.exception_handler(UnsafeName)
async def unusable_name(request, exc):
    """A bad name is the client's mistake, so say so rather than returning 500."""
    return JSONResponse(status_code=400, content={"detail": f"unusable file name: {exc}"})


def run_snakemake():
    ret = os.system("snakemake --cores 4")
    print("RESULT:", ret)


class BiochefWorkflow(BaseModel):
    nodes: list
    edges: list


@app.post("/convert")
async def convert(
    biochef_workflow: str = Form(...),
    files: List[UploadFile] = File(...)
):
    prev_dir = os.getcwd()
    os.makedirs("tmp", exist_ok=True)
    os.chdir("tmp")

    # Save uploaded files. The name is checked rather than trusted: starlette
    # passes the multipart filename through verbatim, so without this an
    # absolute path is written where it says and cwd is not a boundary at all.
    for f in files:
        with open(check_name(f.filename), "wb") as buffer:
            buffer.write(await f.read())

    # Parse workflow
    workflow_dict = json.loads(biochef_workflow)
    workflow = parse_biochef_workflow(workflow_dict)

    # Convert workflow to Snakemake and run
    snakemake = convert_to_snakemake(workflow)
    with open("Snakefile", "w") as f:
        f.write(snakemake)
    run_snakemake()

    # Collect results: all data is base64-encoded
    results = {}
    for node in workflow.nodes:
        if node.id not in results:
            results[node.id] = {}

        for output_name, output in node.outputs.items():
            handle_name = output_name.split("-")[-1]

            with open(output.file, "rb") as file:
                raw = file.read()
                encoded = base64.b64encode(raw).decode("ascii")

            results[node.id][handle_name] = encoded

    os.chdir(prev_dir)

    return results
