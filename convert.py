from fastapi import FastAPI
import errno
import hashlib
import json
import oras.client
import os
import shutil
import stat
import threading
import time
import uuid
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

_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


def _fetch_lock(tool_id):
    """One lock per tool, so two runs never pull the same one at once.

    Without this, concurrent runs raced on the shared staging directory and
    destroyed each other's work: both would rmtree it, both makedirs it, and
    then one would os.replace a directory the other had already moved. Measured
    with twenty simultaneous submissions, nineteen failed --

      [Errno 17] File exists: .../cache/tool.part
      [Errno  2] No such file or directory: .../cache/tool.part

    -- and the one that survived did so by being first. The race predates
    asynchronous runs, since two concurrent /convert calls could always hit it,
    but nothing made concurrency easy to reach until now.

    Per tool rather than one global lock: two different tools have no reason to
    wait for each other, and a pull can be slow.
    """
    with _fetch_locks_guard:
        return _fetch_locks.setdefault(tool_id, threading.Lock())


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


def fetch_tool(tool_id, repo, attempts=4):
    """Pull a bundle into the shared cache and return it.

    Retried, because the cache is shared between processes and the promote is
    not atomic across them. Another process replacing this tool deletes the
    cached directory a moment before putting the new one in its place, and a
    reader arriving in that window finds nothing -- reproduced with six
    processes on one cold cache as

      FileNotFoundError: .../shared-cache/tool/bundle.json

    The per-tool lock covers threads in this process; nothing covers processes,
    and a directory cannot be swapped atomically on POSIX. The state is
    self-healing -- whatever the other process was putting there arrives a
    moment later, verified -- so looking again is the honest answer. Doing it
    properly means an indirection that CAN be swapped atomically, which is a
    change of on-disk layout and its own piece of work.
    """
    for remaining in range(attempts - 1, -1, -1):
        try:
            return _fetch_tool_once(tool_id, repo)
        except (FileNotFoundError, NotADirectoryError):
            if remaining == 0:
                raise
            time.sleep(0.05)


def _fetch_tool_once(tool_id, repo):
    """Pull a bundle into the shared cache and return it.

    The signature is unchanged on purpose: parse_biochef_workflow calls this,
    and threading a workspace through would make the parse know about run
    directories for no reason. It no longer copies or chmods anything -- placing
    the binary is a separate pass that runs once the workspace exists.
    """
    tool_id = check_name(tool_id.split("-")[0])

    outdir = os.path.join(TOOL_CACHE, tool_id)
    target = f"{REGISTRY_URL}/{repo}"

    # Fetched once, and used for both the cache check and the verification of
    # anything pulled, so the two are talking about the same artifact. Two
    # separate fetches made an ordinary push to the same tag, mid-pull, look
    # exactly like tampering.
    manifest = client.get_manifest(client.get_container(target))

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
    with _fetch_lock(tool_id):
        # Re-checked inside the lock. Another run may have pulled this very
        # tool while this one waited, in which case there is nothing to do --
        # and pulling again would be both wasteful and another chance to race.
        if not cache_matches(outdir, manifest, target):
            # Staged, then moved into place, so an interrupted pull cannot leave
            # a half-written bundle that the next run would read as complete.
            #
            # The staging name is unique per attempt. A fixed ".part" was
            # shared between concurrent runs, which is what let them delete
            # each other's work.
            #
            # An earlier version of this comment claimed the unique name also
            # made a second PROCESS sharing the cache safe. It does not, and
            # saying so was worse than saying nothing, because it told the next
            # reader not to look. _fetch_lock is a threading.Lock and reaches
            # only this process; the promote below is two syscalls and is not
            # atomic across processes. Both halves of that are handled where
            # they happen -- _promote tolerates losing the race, and fetch_tool
            # retries a read that lands in the gap -- but neither is the same as
            # being atomic, and a shared cache between replicas is eventually
            # consistent rather than safe by construction.
            staging = f"{outdir}.part.{uuid.uuid4().hex}"
            os.makedirs(staging, exist_ok=True)
            try:
                client.pull(target=target, outdir=staging)
                # Before the staged directory becomes the cached one, and so
                # before anything in it can be copied into a run. A bundle that
                # does not match is removed, never promoted.
                verify_against_manifest(target, staging, manifest=manifest)
                _promote(staging, outdir, manifest, target)
            finally:
                # Whatever happened, this attempt's directory does not outlive
                # it. os.replace moved it on success, so this is a no-op then.
                shutil.rmtree(staging, ignore_errors=True)

        # Read while the lock is still held. The promote deletes the cached
        # directory before putting the new one in its place, so a reader in
        # another THREAD could otherwise find it missing between the two.
        with open(os.path.join(outdir, "bundle.json"), "r") as f:
            bundle = json.load(f)

    tools[tool_id] = bundle
    return bundle


def _promote(staging, outdir, manifest, target, attempts=5):
    """Put a verified staging directory in place, against other processes.

    os.replace will not overwrite a non-empty directory, and the promote is two
    syscalls -- rmtree then replace -- so two processes sharing a cache trip
    over each other. Measured with four processes on one cache: two failed in
    every trial.

    _fetch_lock does not help; it is a threading.Lock and reaches only this
    process. Multi-process is opt-in -- run.sh starts one worker -- but a shared
    cache volume between replicas is a real deployment, so this handles it.

    A clash is not a failure. Whoever won had also passed
    verify_against_manifest against this same manifest, so their copy is as good
    as ours: if what is now in place matches, we use it. Retried because the
    re-check races too, a third process can rmtree between our clash and our
    look, and one attempt left roughly one failure in twenty-four.
    """
    last = None
    for _ in range(attempts):
        shutil.rmtree(outdir, ignore_errors=True)
        try:
            os.replace(staging, outdir)
            return
        except OSError as clash:
                    # Another process promoted the same tool between our rmtree
                    # and our replace. os.replace will not overwrite a
                    # non-empty directory, so it raises ENOTEMPTY (66 on
                    # darwin, 39 on Linux) and this run failed for no reason
                    # other than losing a footrace. Measured at two failures in
                    # four processes, in every trial.
                    #
                    # Their copy is as good as ours -- both passed
                    # verify_against_manifest against the same manifest before
                    # either got here -- so the answer is to check that what is
                    # now in place matches, and use it. If it does not, this is
                    # a real failure and it is raised.
            if clash.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            last = clash
            if cache_matches(outdir, manifest, target):
                return          # someone else put an equivalent copy in place
    raise ToolIntegrityError(
        f"{target}: could not put the verified bundle in place after "
        f"{attempts} attempts; another process kept replacing it. Last: {last}"
    )


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


def rule_name_for(node_id):
    """The snakemake rule name a node is emitted as.

    Extracted so there is one of these rather than two. Attribution of a failing
    step reads "Error in rule <name>:" out of snakemake's output and has to turn
    that back into a node id; a second copy of this transform would go on
    working right up until someone changed the emitter, and then attribute
    failures to nothing at all.
    """
    return node_id.replace(".", "_").replace("-", "_")


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
        result.append(f"rule {rule_name_for(node.id)}:")
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
