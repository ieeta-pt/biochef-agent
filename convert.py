from fastapi import FastAPI
import hashlib
import json
import oras.client
import os
import shutil
import stat
from dataclasses import dataclass, field
from enum import Enum

from dotenv import load_dotenv

import signing
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


class ToolIntegrityError(Exception):
    """The bytes that arrived are not the bytes the registry vouched for."""


# Spelled out rather than imported from oras.defaults, because every test module
# here replaces the oras package with a stub so nothing reaches the registry, and
# importing a submodule of that stub fails. These two values are part of the OCI
# specification rather than of oras.
_ANNOTATION_TITLE = "org.opencontainers.image.title"
_DIR_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"


def _contained_path(root, name, target):
    """Where a manifest title lands, refusing anything that escapes `root`.

    check_name is the wrong instrument here and was the first thing tried. It
    admits one plain file name, so it refused "bin/tool" and "lib/libz.so.1" --
    titles oras itself accepts and sanitises. A bundle laid out in
    subdirectories would have been rejected as an integrity failure.

    Containment is the property that actually matters: the manifest comes from
    the registry, the client chooses the registry, and a title of
    "../../etc/cron.d/x" must not decide which path gets hashed. Resolving and
    checking the prefix allows nesting and still refuses escape.
    """
    resolved = os.path.realpath(os.path.join(root, name))
    base = os.path.realpath(root)
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ToolIntegrityError(
            f"{target}: the manifest names {name!r}, which resolves outside the "
            f"directory the pull was staged in"
        )
    return resolved


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def cache_matches(directory, manifest, target):
    """Whether what is on disk already is what the manifest describes.

    Silent, unlike verify_against_manifest: a mismatch here is a reason to pull
    again, not a reason to fail. The tag may simply have moved to a new version,
    which is the ordinary case and not an incident.
    """
    if not os.path.exists(os.path.join(directory, "bundle.json")):
        return False
    try:
        verify_against_manifest(target, directory, manifest=manifest)
    except ToolIntegrityError:
        return False
    return True


def verify_against_manifest(target, staging, manifest=None):
    """Check every staged file against the digest the manifest claims for it.

    oras never does this. It builds the blob URL from the digest and streams the
    response to a file, so the digest names what was asked for and never what
    arrived (#9). What arrives is then made executable and run.

    A layer's on-disk file IS the blob for ordinary layers, so hashing the staged
    file is the comparison the specification intends. A directory layer is
    different: oras downloads it to a temporary archive and extracts it, so
    nothing on disk is the blob and the digest cannot be checked after the fact.
    That case is refused rather than skipped -- an unverifiable artifact must not
    be quietly treated as a verified one, which is the failure this whole issue
    is about.
    """
    if manifest is None:
        manifest = client.get_manifest(client.get_container(target))
    layers = manifest.get("layers") or []
    if not layers:
        raise ToolIntegrityError(
            f"{target} has no layers in its manifest, so there is nothing to "
            f"verify the pulled files against"
        )

    for layer in layers:
        digest = layer.get("digest")
        if not digest:
            raise ToolIntegrityError(f"{target}: a layer declares no digest")

        if layer.get("mediaType") == _DIR_MEDIA_TYPE:
            raise ToolIntegrityError(
                f"{target}: layer {digest} is a directory archive, which is "
                f"extracted on the way in, so what lands on disk is not the "
                f"blob and its digest cannot be checked. Refusing rather than "
                f"treating it as verified."
            )

        name = (layer.get("annotations") or {}).get(_ANNOTATION_TITLE) or digest
        path = _contained_path(staging, name, target)
        if not os.path.exists(path):
            raise ToolIntegrityError(
                f"{target}: the manifest declares {name!r} but the pull did not "
                f"produce it"
            )

        actual = _sha256_of(path)
        if actual != digest:
            raise ToolIntegrityError(
                f"{target}: {name!r} does not match the manifest. "
                f"expected {digest}, got {actual}. Either the bytes are not the "
                f"ones the registry vouched for, or the tag moved between the "
                f"pull and this check -- an ordinary push to the same tag looks "
                f"identical from here."
            )


# Taken from oras rather than restated: a manifest media type oras would accept
# must not be refused here just because this file has an older list.
try:
    import oras.defaults
    _MANIFEST_TYPES = ", ".join(oras.defaults.default_manifest_accepted_media_types)
except Exception:  # pragma: no cover - a stubbed oras in the test suite
    _MANIFEST_TYPES = "application/vnd.oci.image.manifest.v1+json"


def fetch_manifest(target):
    """The manifest and the digest that names it, from a single fetch.

    oras parses the response and throws it away, so the manifest digest -- which
    is defined as the hash of the exact bytes served, not of anything we could
    re-serialise -- is not recoverable from what get_manifest returns. Signature
    verification needs that digest, and needs it to name the same artifact the
    blobs are pulled from, so the request happens here and both are kept.

    The digest is computed from the bytes rather than taken from the registry's
    Docker-Content-Digest header. The header is advisory and comes from the same
    party as the manifest; if both are present and disagree, that is refused,
    because a registry answering one thing and labelling it another is not a
    situation to pick a winner in.

    Falls back to oras's own accessor for any client that cannot do a raw
    request, returning no digest -- which strict mode refuses, so the fallback
    cannot quietly become a way to skip verification.
    """
    container = client.get_container(target)
    if not (hasattr(client, "do_request") and hasattr(client, "prefix")):
        return client.get_manifest(container), None

    url = f"{client.prefix}://{container.manifest_url()}"
    response = client.do_request(url, "GET", headers={"Accept": _MANIFEST_TYPES})
    if response.status_code != 200:
        raise ToolIntegrityError(
            f"{target}: the registry answered {response.status_code} for its manifest"
        )

    body = response.content
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    advertised = response.headers.get("Docker-Content-Digest")
    if advertised and advertised != digest:
        raise ToolIntegrityError(
            f"{target}: the registry served a manifest whose digest is {digest} "
            f"but labelled it {advertised}"
        )
    return json.loads(body), digest


def fetch_tool(tool_id, repo):
    """Pull a bundle into the shared cache and return it.

    The signature is unchanged on purpose: parse_biochef_workflow calls this,
    and threading a workspace through would make the parse know about run
    directories for no reason. It no longer copies or chmods anything -- placing
    the binary is a separate pass that runs once the workspace exists.
    """
    tool_id = check_name(tool_id.split("-")[0])

    outdir = os.path.join(TOOL_CACHE, tool_id)
    target = f"{REGISTRY_URL}/{repo}"

    # Fetched once, and used for the signature check, the cache check and the
    # verification of anything pulled, so all three are talking about the same
    # artifact. Two separate fetches made an ordinary push to the same tag,
    # mid-pull, look exactly like tampering.
    manifest, manifest_digest = fetch_manifest(target)

    # Before the manifest is used for anything else, because everything else
    # trusts it. cache_matches decides whether the cached bundle is still good
    # by comparing against this manifest, and verify_against_manifest checks
    # pulled blobs against the digests this manifest declares -- so a manifest
    # nobody vouched for makes both of those self-consistent and meaningless.
    signing.check(target, manifest_digest, log=print)

    # On EVERY call, not only on a miss. The cache is what materialise_tools
    # copies into a run and chmods 0700, so verifying only the pull that created
    # it proves something about a moment in the past rather than about the bytes
    # being executed. It is a plain directory, written by this service, and a
    # tool running under the subprocess runner is unconfined and runs as the
    # same user -- so one run can rewrite what every later run executes. That is
    # not a clever attack, it is the ordinary consequence of a shared cache.
    #
    # The cost is a manifest fetch and a local re-hash per tool per request. The
    # blobs are not downloaded again.
    if not cache_matches(outdir, manifest, target):
        # Staged, then moved into place, so an interrupted pull cannot leave a
        # half-written bundle that the next run would read as complete.
        staging = outdir + ".part"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        client.pull(target=target, outdir=staging)
        # Before the staged directory becomes the cached one, and so before
        # anything in it can be copied into a run. A bundle that does not match
        # is left in .part and removed, never promoted.
        try:
            verify_against_manifest(target, staging, manifest=manifest)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
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
