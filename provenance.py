"""What a run was, written down (#18).

E5's acceptance is that the manifest be sufficient to re-execute the run and
reach the same outputs. That sets the contents: not a log of what happened, but
the identity of everything that decided the answer -- the workflow, the exact
bundles, the exact inputs, and what came out.

The vocabulary is the hub's, deliberately. It publishes
`biochef.build-evidence.v1` with a `schema` field and digests throughout, and
signs artifacts as in-toto statements. A run manifest inventing its own terms
would be a second provenance format inside one project, so this one mirrors the
shape and carries the hub's evidence forward by reference rather than restating
it in different words.

What it does NOT claim is that re-execution is guaranteed. A workflow whose tool
reads the clock, or the network, or a file this manifest cannot name, will not
reproduce -- and saying so is more useful than a document that implies otherwise.
The manifest records what was fixed; whether that was everything is a property of
the tools.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

SCHEMA = "biochef.run-manifest.v1"
"""Versioned like the hub's, and for the same reason.

A consumer that finds a schema it does not know should say so rather than read
fields that may have moved.
"""

MANIFEST_NAME = "run.json"


def _digest_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_digest(document):
    """A digest of a JSON document that does not depend on key order.

    The same trick the hub uses for an operation's digest: sorted keys and no
    incidental whitespace, so two equal documents hash the same however they
    were serialised.
    """
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def build(*, run_id, workflow_document, workflow, ws, inputs, outputs,
          bundles, exit_code, started_at, finished_at, runner, image=None):
    """The manifest for one finished run.

    Every digest is computed from what is on disk at the moment this is called,
    which is after the run and before the workspace is released -- so an input a
    tool overwrote is recorded as what the tool left, not as what arrived. That
    is the honest reading: re-execution needs the bytes the tools actually saw,
    and if a tool rewrote its own input then the manifest cannot pretend
    otherwise.
    """
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "finished_at": finished_at,
        "workflow": {
            # By digest, not by copy. The document can be large and the client
            # already has it; what a manifest needs is to say WHICH one.
            "digest": _canonical_digest(workflow_document),
            "nodes": [
                {"id": node.id, "bin": node.bin,
                 "inputs": sorted(node.inputs),
                 "outputs": sorted(node.outputs)}
                for node in workflow.nodes
            ],
        },
        "tools": bundles,
        "inputs": _identities(ws, sorted(inputs)),
        "outputs": {
            node_id: _identities(ws, sorted(handles.values()))
            for node_id, handles in outputs.items()
        },
        "execution": {
            "runner": runner,
            "image": image,
            "exit_code": exit_code,
        },
    }


def _identities(ws, names):
    """The digest of each named file in the workspace.

    Read through the workspace rather than by path, so a tool that replaced one
    with a symlink cannot have the target's digest recorded as its own (#41) --
    the manifest would otherwise claim a run consumed something it did not.
    """
    identities = {}
    for name in names:
        try:
            with ws.open_read(name) as handle:
                digest = hashlib.sha256()
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            identities[name] = "sha256:" + digest.hexdigest()
        except Exception:                            # noqa: BLE001
            # A file that is not there, or is not a regular file, is recorded as
            # such. Omitting it would leave a reader to guess whether it was
            # absent or merely unmentioned.
            identities[name] = None
    return identities


def write(ws, manifest):
    """Put the manifest in the run's own workspace.

    Beside the outputs it describes, so retention keeps them together and
    releasing a run releases both. Written last, because it records the exit
    code.
    """
    ws.write_bytes(MANIFEST_NAME,
                   (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return MANIFEST_NAME
