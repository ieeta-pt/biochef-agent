from fastapi import FastAPI
import json
import oras.client
import os
import shutil
import stat
from dataclasses import dataclass, field
from enum import Enum

from dotenv import load_dotenv

from workspace import check_name

load_dotenv()

REGISTRY_URL = os.getenv("REGISTRY_URL", "registry.biochef.app")
REGISTRY_USERNAME = os.getenv("REGISTRY_USERNAME", " ")
REGISTRY_PASSWORD = os.getenv("REGISTRY_PASSWORD", " ")
REGISTRY_INSECURE = os.getenv("REGISTRY_INSECURE", "false").lower() == "true"
ORAS_AUTH_BACKEND = os.getenv("ORAS_AUTH_BACKEND", "token")

app = FastAPI()
client = oras.client.OrasClient(
    hostname=REGISTRY_URL,
    insecure=REGISTRY_INSECURE,
    auth_backend=ORAS_AUTH_BACKEND
)
client.login(
    username=REGISTRY_USERNAME,
    password=REGISTRY_PASSWORD
)

class IOMode(Enum):
    STDIN = "stdin"
    STDOUT = "stdout"
    FILE = "file"


@dataclass
class IO:
    file: str = ""
    """
    this is the file that the tools that are connected 
    expect to have the input/output
    """

    mode: IOMode = None
    """
    the mode in which the input/output is received
    """

    hardcoded_file: str = ""
    """
    some tools have an hardcoded file that type write to
    this file will then be copied to the actual file above when running the workflow
    """

    flag: str = ""
    """
    some tools receive input/output through a flag argument
    """


@dataclass
class Param:
    name: str = ""
    value: str = ""
    flag: str = ""


@dataclass
class Node:
    """Information about each node of the workflow"""

    id: str = ""
    bin: str = ""
    inputs: dict[str, IO] = field(default_factory=dict)
    outputs: dict[str, IO] = field(default_factory=dict)
    parameters: dict[str, Param] = field(default_factory=dict)


@dataclass
class Workflow:
    nodes: list[Node] = field(default_factory=list)


TOOL_CACHE = os.path.realpath(os.getenv("BIOCHEF_TOOL_CACHE", "tool-cache"))
"""Where pulled bundles live, shared by every run.

Splitting the cache out of the run directory is forced by giving each run its
own directory, not a tidy-up. fetch_tool memoises and returns early on a hit, so
it never re-copied the binary; with one shared directory that made the copy
survive between runs, and with a fresh directory per run it would simply be
missing on the second. The pull is cached here, and materialise_tools puts a
copy in whichever workspace needs it.
"""

tools = {}


def fetch_tool(tool_id, repo):
    """Pull a bundle into the shared cache and return it.

    The signature is unchanged on purpose: parse_biochef_workflow calls this,
    and threading a workspace through would make the parse know about run
    directories for no reason. It no longer copies or chmods anything -- placing
    the binary is a separate pass that runs once the workspace exists.
    """
    tool_id = check_name(tool_id.split("-")[0])

    if tool_id in tools:
        return tools[tool_id]

    outdir = os.path.join(TOOL_CACHE, tool_id)
    if not os.path.exists(os.path.join(outdir, "bundle.json")):
        # Staged, then moved into place, so an interrupted pull cannot leave a
        # half-written bundle that the next run would read as complete. It is
        # also the point where a digest would be checked (#9).
        staging = outdir + ".part"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        client.pull(target=f"{REGISTRY_URL}/{repo}", outdir=staging)
        shutil.rmtree(outdir, ignore_errors=True)
        os.replace(staging, outdir)

    with open(os.path.join(outdir, "bundle.json"), "r") as f:
        bundle = json.load(f)

    tools[tool_id] = bundle
    return bundle


def expected_uploads(workflow):
    """The files the client has to supply, derived from the workflow itself.

    Every intermediate file is named for the edge that carries it, so the set a
    run consumes and the set it produces are both known before anything runs.
    What is consumed but not produced by any node is what has to arrive as an
    upload; everything else the run creates for itself.

    This is the second of the two gates described in workspace.py. check_name
    decides whether a name is a usable file name at all; this decides whether it
    is one this run has any business receiving. Each catches what the other
    cannot -- "samtools" is a perfectly good file name, and an output slot is a
    name the workflow does declare.
    """
    produced = {name for node in workflow.nodes for name in node.outputs}
    consumed = {name for node in workflow.nodes for name in node.inputs}
    return consumed - produced


def materialise_tools(workflow, ws):
    """Put a copy of each tool's binary into this run's workspace.

    Separate from fetch_tool so the cache is pulled once and every run still
    gets its own copy. Done before the uploads, so that an upload named after a
    binary is refused by O_EXCL rather than silently replacing it.

    Once per BINARY, not once per node. Several operations routinely share one
    executable -- 80 of the 176 in the catalogue do, including all 15 samtools
    operations and all 20 seqtk ones -- so a workflow as ordinary as
    "samtools sort" into "samtools index" has two nodes naming the same binary.
    Placing it per node made the second one fail O_EXCL with FileExistsError and
    the whole request 500. The old fetch_tool hid this behind its memo, which
    returned early and skipped the copy; splitting the cache out lost that and
    nothing replaced it.
    """
    placed = set()
    for node in workflow.nodes:
        name = check_name(node.bin)
        if name in placed:
            continue
        source = os.path.join(TOOL_CACHE, check_name(node.id.split("-")[0]), name)
        ws.place_executable(source, name)
        placed.add(name)


def get_node_data(node_id, node_list):
    return next(node for node in node_list if node["id"] == node_id)


def parse_biochef_workflow(biochef_workflow):
    node_list, edge_list = biochef_workflow["nodes"], biochef_workflow["edges"]
    new_workflow: Workflow = Workflow()

    for node in node_list:
        node_id, node_type = node["id"], node["type"]
        if node_type != "workflowNode":
            continue

        tool_info = fetch_tool(node_id, node["data"]["repo"])

        new_node: Node = Node(id=node_id, bin=tool_info["bin"])

        connections = [e for e in edge_list if node_id in (e["target"], e["source"])]
        for connection in connections:
            source, source_handle, target, target_handle = (
                connection["source"], connection.get("sourceHandle"),
                connection["target"], connection.get("targetHandle"),
            )

            _name = f"{source}-{source_handle}"

            def build_io(info):
                return IO(
                    f"{_name}",
                    IOMode(info.get("mode")),
                    info.get("filename"),
                    info.get("flag"),
                )

            is_input_connection = node_id == target
            if is_input_connection:
                input_info = next(
                    i for i in tool_info["io"]["inputs"] if i["name"] == target_handle)
                new_node.inputs[_name] = build_io(input_info)
            else:
                output_info = next(
                    i for i in tool_info["io"]["outputs"] if i["name"] == source_handle)
                new_node.outputs[_name] = build_io(output_info)

        for param_key, param in node["data"]["paramValues"].items():
            if param.get("enabled") != True:
                continue
            param_info = next(
                p for p in tool_info["parameters"] if p["name"] == param_key)

            new_param: Param = Param(
                param_key, param["value"], param_info.get("flag")
            )

            new_node.parameters[param_key] = new_param

        new_workflow.nodes.append(new_node)

    return new_workflow


def convert_to_snakemake(workflow: Workflow):
    result = []
    result.append("rule all:\n    input:")

    for node in workflow.nodes:
        for output in node.outputs.values():
            result.append(f"        \"{output.file}\",")

    for node in workflow.nodes:
        # print(node)
        result.append(f"rule {node.id.replace(".", "_").replace("-", "_")}:")
        cmd = [f"./{node.bin}"]
        extra_cms = []

        for param_name, param in node.parameters.items():
            if param.flag:
                cmd.append(param.flag)
            cmd.append(param.value)

        result.append("    input:")
        i = 0
        for input_name, input in node.inputs.items():
            input_var = f"i_{i}"
            result.append(f"        {input_var}=\"{input.file}\",")
            if input.mode == IOMode.STDIN:
                cmd.append("<")
                cmd.append(f"{{input.{input_var}}}")
            elif input.mode == IOMode.FILE:
                if input.flag: cmd.append(f"{input.flag}")
                cmd.append(f"{{input.{input_var}}}")
            i += 1

        result.append("    output:")
        i = 0
        for output_name, output in node.outputs.items():
            output_var = f"o_{i}"
            result.append(f"        {output_var}=\"{output.file}\",")
            if output.mode == IOMode.STDOUT:
                cmd.append(">")
                cmd.append(f"{{output.{output_var}}}")
            elif output.mode == IOMode.FILE:
                if output.flag: cmd.append(f"{output.flag}")
                cmd.append(f"{{output.{output_var}}}")
                if output.hardcoded_file and not output.flag:
                    extra_cms.append(f"        cp {output.hardcoded_file} {{output.{output_var}}}")
            i += 1

        result.append(f"    shell:")
        result.append(f"        \"\"\"")
        result.append(f"        {" ".join(cmd)}")
        for command in extra_cms:
            result.append(command)
        result.append(f"        \"\"\"")

    return "\n".join(result)

# with open("test.json") as file:
#     workflow = parse_biochef_workflow(json.loads(file.read()))
#     print(workflow)
#     print(convert_to_snakemake(workflow))
