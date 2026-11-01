from fastapi import FastAPI
import copy
import fcntl
import hashlib
import json
import keyword
import shlex
import oras.client
import os
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from intermediate import IO, IOMode, Node, Param, Workflow

from dotenv import load_dotenv

import signing
import evidence_verification
from workspace import UnsafeName, check_name

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

WRITE_INTERMEDIATE = os.getenv("BIOCHEF_WRITE_INTERMEDIATE", "false").lower() == "true"
"""Feature flag for the intermediate artifact (#2).

Off: the Snakefile is generated from the document held in memory, exactly as
before. On: the document is written to intermediate.json, read back, validated
against the schema, and the Snakefile is generated from that -- so the Snakefile
becomes a function of a file on disk rather than of the request body. The two
paths are asserted byte-identical over every operation in the catalogue by
tests/test_intermediate_roundtrip.py, which is what makes turning it on safe.
"""

INTERMEDIATE_FILENAME = "intermediate.json"


class IntermediateMismatch(Exception):
    """The intermediate document read back is not the one written."""


def write_intermediate(workflow: Workflow, path: str) -> str:
    """Write the document, as a new file only.

    Exclusive and without following a symlink, like every file a run creates
    in its workspace: nothing put at the path first, a file or a link to one
    elsewhere, can stand in for the document or be written through.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(workflow.to_json())
    return path


def read_intermediate(path: str) -> Workflow:
    """Read the document back, validating it on the way in.

    A document that does not match the schema raises here rather than producing
    a nonsense Snakefile further downstream. Only a regular file is read, and
    not through a symlink.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeName(f"{path!r} is not a regular file")
    with os.fdopen(fd) as f:
        return Workflow.from_json(f.read())


def through_intermediate(workflow: Workflow, path: str) -> Workflow:
    """Round trip through the supplied file when enabled.

    The executor supplies a path inside its run workspace. There is no default:
    a relative name resolves against the process's directory, which every run in
    flight shares, so a caller that forgot the path would put concurrent runs'
    documents in one place.

    The copy read back must be the one written. The document carries each
    node's registry evidence, which materialise_tools checks the executable
    against and skips for a node without it, so a copy changed on disk could
    switch that check off. It is compared as serialised, since the order of
    inputs, outputs and parameters is the order of the command line.
    """
    if not WRITE_INTERMEDIATE:
        return workflow
    write_intermediate(workflow, path)
    reread = read_intermediate(path)
    if reread.to_json() != workflow.to_json():
        raise IntermediateMismatch(f"{path!r} read back differs from the document written")
    return reread


class ToolBundle(dict):
    """A bundle plus the evidence snapshot observed when it was fetched."""

    def __init__(self, bundle, provenance):
        super().__init__(bundle)
        self.provenance = copy.deepcopy(provenance)


TOOL_CACHE = os.path.realpath(os.getenv("BIOCHEF_TOOL_CACHE", "tool-cache"))
"""Where pulled bundles live, shared by every run.

Each run has its own workspace, while downloaded bundles can be reused. On
every fetch, the cached files are checked against the registry manifest; a
matching entry avoids another pull. materialise_tools copies the executable
into whichever run workspace needs it.
"""

tools = {}

provenance = {}
"""What each pulled bundle was, kept for the run manifest (#18)."""

_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


@contextmanager
def _fetch_lock(tool_id):
    """Lock one local cache entry across cooperating threads and processes."""
    with _fetch_locks_guard:
        thread_lock = _fetch_locks.setdefault(tool_id, threading.Lock())

    with thread_lock:
        os.makedirs(TOOL_CACHE, exist_ok=True)
        # Keep this file: recreating it could let processes lock different inodes.
        lock_path = os.path.join(TOOL_CACHE, f".{tool_id}.lock")
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


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
    """The manifest and the digest that names it, from a single fetch, along with the raw bytes.

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
        return client.get_manifest(container), None, None

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
    requested = getattr(container, "digest", None)
    if requested and requested != digest:
        raise ToolIntegrityError(
            f"{target}: the registry served manifest {digest}, but the requested "
            f"immutable reference names {requested}"
        )
    return json.loads(body), digest, body


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
    current_mode = signing.mode()
    if current_mode == signing.STRICT and not signing.is_immutable(target):
        raise signing.SignatureError(
            f"{target}: strict verification requires the caller to select an "
            "immutable OCI manifest digest"
        )

    with _fetch_lock(tool_id):
        manifest, manifest_digest, raw_manifest = fetch_manifest(target)
        try:
            subject = signing.immutable_reference(target, manifest_digest)
        except signing.SignatureError:
            subject = None

        signing.check(target, manifest_digest, log=print)
        evidence = evidence_verification.check(
            subject,
            raw_manifest,
            client,
            fetch_manifest,
            log=print,
        )

        pull_target = subject if current_mode != signing.OFF and subject else target
        if not cache_matches(outdir, manifest, pull_target):
            staging = f"{outdir}.part.{uuid.uuid4().hex}"
            os.makedirs(staging)
            try:
                client.pull(target=pull_target, outdir=staging)
                verify_against_manifest(pull_target, staging, manifest=manifest)
                evidence_verification.verify_pulled(
                    staging, subject, tool_id, evidence, log=print
                )
                shutil.rmtree(outdir, ignore_errors=True)
                os.replace(staging, outdir)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        else:
            evidence_verification.verify_pulled(
                outdir, subject, tool_id, evidence, log=print
            )

        with open(os.path.join(outdir, "bundle.json"), "r") as f:
            bundle = json.load(f)

        # Kept for the run manifest (#18). The registry publishes three
        # artifacts per bundle and verify_against_manifest has just hashed all
        # of them against the manifest -- so their identity is established here
        # and was, until now, thrown away.
        evidence = _bundle_evidence(outdir, manifest, target)

        # Inside the lock, which is where master moved them: the cache entry and
        # what this process believes about it are set together or a second
        # fetcher can read one without the other.
        tools[tool_id] = bundle
        provenance[tool_id] = evidence
        return ToolBundle(bundle, evidence)

def _bundle_evidence(directory, manifest, target):
    """What this bundle is, in the terms the registry stated it.

    Digests come from the MANIFEST rather than from hashing the files again.
    They are the same numbers -- verify_against_manifest has just checked that --
    but taking them from the manifest records what the registry asserted, which
    is the thing a reader of the manifest would want to compare against.
    """
    artifacts = {}
    for layer in manifest.get("layers") or []:
        title = (layer.get("annotations") or {}).get(_ANNOTATION_TITLE)
        if title and layer.get("digest"):
            artifacts[title] = layer["digest"]

    evidence = {"target": target, "artifacts": artifacts}

    # The hub's own build evidence, if this bundle carries it. Referenced by
    # digest AND summarised, so a manifest is readable without the registry
    # while still saying exactly which document it means.
    build = os.path.join(directory, "build-evidence.json")
    if os.path.exists(build):
        try:
            with open(build, "r") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return evidence
        evidence["build_evidence"] = {
            "schema": document.get("schema"),
            "generated_at": document.get("generated_at"),
            "hub": document.get("hub"),
            "recipe": {
                key: (document.get("recipe") or {}).get(key)
                for key in ("id", "name", "version", "digest")
            },
            "operation": document.get("operation"),
        }
    return evidence


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
    consumed = {io.file for node in workflow.nodes for io in node.inputs.values()}
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
    by_name = {}
    for node in workflow.nodes:
        name = check_name(node.bin)
        by_name.setdefault(name, []).append(node)

    for name, nodes in by_name.items():
        tool_id = check_name(nodes[0].id.split("-")[0])
        source = os.path.join(TOOL_CACHE, tool_id, name)
        with _fetch_lock(tool_id):
            ws.place_executable(source, name)

        evidenced_nodes = [node for node in nodes
                           if getattr(node, "provenance", {})]
        if not evidenced_nodes:
            continue

        digest = hashlib.sha256()
        try:
            with ws.open_read(name, regular_only=True) as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except (OSError, ValueError) as error:
            raise ToolIntegrityError(
                f"staged tool {name!r} could not be read as a regular file"
            ) from error
        actual = "sha256:" + digest.hexdigest()

        for node in evidenced_nodes:
            evidence = node.provenance
            expected = (evidence.get("artifacts") or {}).get(name)
            if not expected:
                raise ToolIntegrityError(
                    f"tool {node.id!r} has no registry digest for executable {name!r}"
                )
            if expected != actual:
                raise ToolIntegrityError(
                    f"tool {node.id!r} executable {name!r} has digest {actual}, "
                    f"expected {expected}"
                )


def sh_literal(value) -> str:
    """A token that must reach the shell as one argument, exactly as written.

    Two escapes, applied in this order, for two different readers.

    `shlex.quote` makes the value a single shell word, so a value of `; id`
    becomes `'; id'` and the shell reads text where it used to read a second
    command. `str()` first because a value is whatever JSON decoded -- a recipe
    default of `2` arrives as an int, and joining that raised TypeError (#37).

    Then the braces are doubled, because Snakemake formats the shell block
    before any shell sees it. Verified against snakemake 9.21.0: a lone `{` in a
    shell block is a NameError and the run exits 1, while `{{` renders as `{`.
    Doubling also means a value containing `{input.i_0}` is written out as text
    instead of expanding to another rule's field.
    """
    return shlex.quote(str(value)).replace("{", "{{").replace("}", "}}")


def sh_field(reference: str) -> str:
    """A Snakemake field reference, quoted by Snakemake when it expands.

    `:q` applies shell quoting to whatever the field expands to, so a path
    containing a space stays one argument rather than becoming two. Verified
    against snakemake 9.21.0.

    The braces here are deliberately NOT doubled -- they are what makes this a
    reference rather than literal text.
    """
    return f"{{{reference}:q}}"


def py_string(value: str) -> str:
    """A Python string literal for the input:/output: sections of a rule.

    Those sections are Python, not shell, so a name containing a quote would
    end the literal and let the rest be read as code. json.dumps produces the
    same bytes as the hand-written f-string for every name in the catalogue and
    stays valid for names that are not.
    """
    return json.dumps(value)


def rule_name_for(node_id: str) -> str:
    """Snakemake rule names are Python identifiers.

    A node id is "{operation.id}-{timestamp}", so both separators have to go.
    The result is checked rather than assumed: a node id is client-supplied, and
    one containing a newline would otherwise write new lines into the Snakefile
    at the point where a rule is declared -- which no amount of shell quoting
    further down would help with.
    """
    name = node_id.replace(".", "_").replace("-", "_")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"node id {node_id!r} does not make a usable rule name")
    return name


def get_node_data(node_id, node_list):
    return next(node for node in node_list if node["id"] == node_id)


class WorkflowInputError(ValueError):
    pass


def parse_biochef_workflow(biochef_workflow):
    node_list, edge_list = biochef_workflow["nodes"], biochef_workflow["edges"]
    new_workflow: Workflow = Workflow()

    for node in node_list:
        node_id, node_type = node["id"], node["type"]
        if node_type != "workflowNode":
            continue

        tool_info = fetch_tool(node_id, node["data"]["repo"])

        new_node: Node = Node(
            id=node_id, bin=tool_info["bin"],
            provenance=copy.deepcopy(getattr(tool_info, "provenance", {})),
        )

        connections = [e for e in edge_list if node_id in (e["target"], e["source"])]

        def build_io(info, file_name):
            # `.get` returning None where a recipe omits the key is kept as None
            # rather than flattened to "": the two mean different things to the
            # frontend and both occur in the catalogue, so the model records
            # which one the recipe actually said.
            return IO(
                file=file_name,
                mode=IOMode(info.get("mode")),
                hardcoded_file=info.get("filename"),
                flag=info.get("flag"),
            )

        # Inputs, in the order the recipe declares them and keyed by which
        # declared input each one fills.
        #
        # Both were previously taken from the edges: the dict was keyed by
        # f"{source}-{source_handle}" and built by walking the edge list. Two
        # things went wrong. Order followed however the client happened to list
        # its edges rather than what the tool expects (#31). And one source
        # feeding two inputs of the same tool produced the same key twice, so
        # the second overwrote the first and the tool was invoked an argument
        # short with nothing saying so (#32).
        #
        # The file name is unchanged -- it is still f"{source}-{source_handle}",
        # because that is the file the upstream node writes. Only the key and
        # the order change.
        incoming = [e for e in connections if e["target"] == node_id]
        for input_info in tool_info["io"]["inputs"]:
            handle = input_info["name"]
            connection = next(
                (e for e in incoming if e.get("targetHandle") == handle), None)
            if connection is None:
                continue  # a declared input nothing is wired to
            file_name = f"{connection['source']}-{connection.get('sourceHandle')}"
            new_node.inputs[handle] = build_io(input_info, file_name)

        # Outputs keep their file-derived key. Nothing reads it as an identity
        # -- main.py splits the handle back off it -- and changing it would be a
        # second, unrelated break of the same contract.
        for connection in connections:
            if connection["target"] == node_id:
                continue
            source_handle = connection.get("sourceHandle")
            file_name = f"{connection['source']}-{source_handle}"
            output_info = next(
                i for i in tool_info["io"]["outputs"] if i["name"] == source_handle)
            new_node.outputs[file_name] = build_io(output_info, file_name)

        # Walk the recipe, not the request. paramValues is a JSON object, so
        # its order is whatever the client serialised -- the same workflow sent
        # twice with different key order produced two different commands (#44).
        # The recipe's parameter list is the one thing that says what order the
        # tool expects, and it is what the frontend walks
        # (RecipePanel.js:584).
        #
        # A parameter the recipe does not declare is now ignored rather than
        # raising StopIteration from inside the parse. It cannot be emitted --
        # there is no flag or type for it -- and a request carrying one is not a
        # reason to fail with an exception that names nothing.
        supplied = node["data"]["paramValues"]
        for param_info in tool_info["parameters"]:
            param_key = param_info["name"]
            param = supplied.get(param_key)
            enabled = param is not None and param.get("enabled") == True

            # A required parameter is resolved from the recipe rather than
            # depending on the client to volunteer it (#33).
            #
            # Deciding purely on the client's `enabled` flag meant a subcommand
            # survived only because RecipePanel.js initialises required
            # parameters as enabled. Any client that does not replicate that
            # produced `./samtools {input}` -- not samtools doing the wrong
            # thing, samtools with no subcommand at all. 65 of the catalogue's
            # 73 hidden parameters are subcommand selectors.
            #
            # Only where the recipe declares a default. The other 29 required
            # parameters are visible ones a user genuinely has to supply --
            # sed's script, tr's set1 -- and there is nothing to fall back on;
            # inventing a value for those would be worse than rejecting the run.
            if not enabled:
                if not param_info.get("required"):
                    continue
                if param_info.get("type") == "flag":
                    # A required flag needs no default: its presence IS the
                    # value, and the emitter drops the value for this type
                    # anyway. Requiring a default here would have skipped
                    # samtools fixmate's -m, which the recipe declares required
                    # and gives nothing to fall back on because there is nothing
                    # to fall back to.
                    value = ""
                else:
                    value = param_info.get("default")
            else:
                value = (param.get("value") if param_info.get("required")
                         else param["value"])
                # An enabled required parameter with nothing in it falls back to
                # the recipe too, so a client that sends the key but leaves the
                # value empty is no worse off than one that omits it entirely.
                if (value is None or value == "") and param_info.get("required") \
                        and param_info.get("default") is not None:
                    value = param_info["default"]

            if param_info.get("required"):
                if param_info.get("type") == "flag":
                    value = ""
                elif value is None or value == "":
                    raise WorkflowInputError(
                        f"node {node_id!r} is missing required parameter {param_key!r}"
                    )

            new_param: Param = Param(
                name=param_key,
                value=value,
                flag=param_info.get("flag"),
                type=param_info.get("type"),
            )

            new_node.parameters[param_key] = new_param

        new_workflow.nodes.append(new_node)

    return new_workflow


def convert_to_snakemake(workflow: Workflow, step_logs=None):
    if step_logs is not None and len(step_logs) != len(workflow.nodes):
        raise ValueError("one pair of log names is required for each node")
    if step_logs is not None:
        names = [check_name(name) for pair in step_logs for name in pair]
        if len(set(names)) != len(names):
            raise ValueError("step log names must be distinct")
    result = []
    result.append("rule all:\n    input:")

    for node in workflow.nodes:
        for output in node.outputs.values():
            result.append(f"        {py_string(output.file)},")

    for index, node in enumerate(workflow.nodes):
        result.append(f"rule {rule_name_for(node.id)}:")
        cmd = [sh_literal(f"./{node.bin}")]
        extra_cms = []

        for param_name, param in node.parameters.items():
            # Omit empty typed values with their flags, matching the browser.
            if param.type not in (None, "flag") and param.value == "":
                continue
            if param.flag:
                cmd.append(sh_literal(param.flag))

            if param.type == "flag":
                # A flag parameter is its flag. It has no value, and emitting
                # one would hand the tool an argument it does not take -- which
                # is what the frontend has always done (RecipePanel.js:590) and
                # what the model can now express. 469 of the catalogue's 1107
                # parameters are this type.
                continue

            # An empty value stays absent rather than becoming an empty
            # argument: shlex.quote("") is '', which is a real argument the tool
            # never used to receive.
            if param.value == "":
                continue

            # In declared position, flagged or not. An unflagged value is NOT
            # held back the way an unflagged file is: every one of them in the
            # catalogue is a hidden subcommand declared first -- "consensus" in
            # `bcftools consensus -c ...` -- and moving it after the flags
            # breaks the invocation. #49 argued the opposite from the getopt
            # rule that applies to filenames; it does not apply here, and the
            # frontend has always emitted these in place.
            cmd.append(sh_literal(param.value))

        # Arguments with no flag are held back and appended after every flagged
        # one. A bare filename ahead of a flag makes a getopt-style parser stop
        # scanning, so the flags that follow are never seen: tn93 given
        # "in.fa -o out.txt" prints its usage and exits 1, while
        # "-o out.txt in.fa" runs. The frontend orders them the same way.
        trailing = []
        # Redirections are kept apart from the arguments and written last. The
        # shell accepts them anywhere, but "tool > out.txt in.fa" reads as
        # though the input were part of the redirect.
        redirects = []

        result.append("    input:")
        i = 0
        for input_name, input in node.inputs.items():
            input_var = f"i_{i}"
            result.append(f"        {input_var}={py_string(input.file)},")
            if input.mode == IOMode.STDIN:
                redirects.append(f"< {sh_field(f'input.{input_var}')}")
            elif input.mode == IOMode.FILE:
                if input.flag:
                    cmd.append(sh_literal(input.flag))
                    cmd.append(sh_field(f"input.{input_var}"))
                else:
                    trailing.append(sh_field(f"input.{input_var}"))
            i += 1

        result.append("    output:")
        i = 0
        for output_name, output in node.outputs.items():
            output_var = f"o_{i}"
            result.append(f"        {output_var}={py_string(output.file)},")
            if output.mode == IOMode.STDOUT:
                redirects.append(f"> {sh_field(f'output.{output_var}')}")
            elif output.mode == IOMode.FILE:
                if output.flag:
                    cmd.append(sh_literal(output.flag))
                    cmd.append(sh_field(f"output.{output_var}"))
                else:
                    trailing.append(sh_field(f"output.{output_var}"))
                if output.hardcoded_file and not output.flag:
                    extra_cms.append(
                        f"cp {sh_literal(output.hardcoded_file)} "
                        f"{sh_field(f'output.{output_var}')}"
                    )
            i += 1

        cmd.extend(trailing)
        cmd.extend(redirects)

        # Assembled outside the f-string. Nesting the same quote inside one is
        # only legal from Python 3.12 (PEP 701), and relying on that here made
        # the whole module fail to import on 3.11 and earlier -- for a line that
        # reads no better either way.
        command_line = " ".join(cmd)

        # A Python string literal, not a triple-quoted block.
        #
        # There is one more reader than the shell and Snakemake's formatter, and
        # it is the outermost one: a Snakefile IS Python source, so Python parses
        # this literal before anything else sees it. In a """...""" block that
        # meant two escapes:
        #
        #   a value containing a backslash escape was decoded by Python AFTER
        #   shlex.quote had finished, so "x\x27; touch PWNED; \x27" became a real
        #   quote and broke out of the shell quoting;
        #
        #   a value containing three double quotes ended the literal, and the
        #   rest of it ran as Python at parse time -- arbitrary code on the agent
        #   host, before any tool started, with the run still reporting success.
        #
        # json.dumps emits a literal in which neither is expressible: every
        # backslash and quote it produces is already escaped for Python, and the
        # value it decodes to is exactly the text that was assembled. The brace
        # doubling done by sh_literal survives untouched, so Snakemake's
        # formatter still reads what it should.
        shell_body = "\n".join([command_line] + extra_cms)
        if step_logs is not None:
            stdout_name, stderr_name = step_logs[index]
            result.append(
                f"    log: stdout={py_string(check_name(stdout_name))}, "
                f"stderr={py_string(check_name(stderr_name))}"
            )
            shell_body = "(\n" + shell_body + "\n) > {log.stdout:q} 2> {log.stderr:q}"
        result.append("    shell:")
        result.append(f"        {py_string(shell_body)}")

    return "\n".join(result)

# with open("test.json") as file:
#     workflow = parse_biochef_workflow(json.loads(file.read()))
#     print(workflow)
#     print(convert_to_snakemake(workflow))
