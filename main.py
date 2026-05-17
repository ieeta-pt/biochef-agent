from convert import *
from fastapi import FastAPI, UploadFile, File, Form
from typing import List
import json
from pydantic import BaseModel
import os
import base64

app = FastAPI()


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

    # Save uploaded files
    for f in files:
        with open(f.filename, "wb") as buffer:
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
