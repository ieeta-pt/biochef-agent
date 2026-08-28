"""A GA4GH WES surface over the runs this service already has (#24, F5).

B1 adopted the WES `RunState` vocabulary rather than inventing one, and the
cancel endpoint was put at WES's path, both so that this layer would be a
mapping and not a rewrite. It is: every endpoint here reads the same RunStore
the bespoke API reads, and a run submitted through either is the same run.

The bespoke endpoints stay. The editor uses them, third parties should not have
to, and removing them to prove a point about standards would break the one
client this service has.

## The part where a WES server can lie

`service-info` is a claim about what this server will accept, and the tempting
claim is `CWL` and `WDL`, because those are the words that make a WES server
look interoperable. This service runs BioChef workflow documents, converted to
Snakemake. It cannot run CWL. Advertising it would mean every conformant client
discovers this server, submits, and fails -- which is worse than not being
discovered, because the failure arrives after the data has been uploaded.

So the declared type is BIOCHEF, and anything else is refused by name with a
message saying what IS accepted. A client that speaks WES can read
`service-info`, see it cannot use this server, and go elsewhere. That is the
whole purpose of `service-info`.
"""

BASE = "/ga4gh/wes/v1"

# The specification this maps onto. Not a claim of full conformance -- see
# `UNIMPLEMENTED` -- but the version whose shapes these are.
WES_VERSION = "1.1.0"

# What this server actually runs. See the module docstring: naming a type this
# service cannot execute would make conformant clients fail after uploading.
WORKFLOW_TYPE = "BIOCHEF"
WORKFLOW_TYPE_VERSION = "1"

# Stated in service-info under `tags`, because a client that discovers this
# server deserves to know where the standard surface stops before it depends on
# a part of it that is not here.
UNIMPLEMENTED = (
    "workflow_url may only name an attachment; remote workflow URLs are not "
    "fetched",
    "run listing is not paginated and ignores page_size and page_token",
    "task_logs carries one entry per workflow step, without per-task commands",
)


class UnsupportedWorkflow(Exception):
    """The submission names a workflow type this server does not run."""


class MissingWorkflow(Exception):
    """The submission does not say which attachment is the workflow."""


def check_type(workflow_type, workflow_type_version):
    """Refuse by name, and say what would have been accepted.

    A 400 reading "unsupported" tells a client nothing it can act on. Naming the
    accepted type means the next request can be right.
    """
    if workflow_type != WORKFLOW_TYPE:
        raise UnsupportedWorkflow(
            f"this server runs {WORKFLOW_TYPE!r} workflows and not "
            f"{workflow_type!r}; see {BASE}/service-info"
        )
    # A version is required by the specification and there is exactly one, so an
    # empty value is accepted as meaning it rather than refused on a technicality.
    if workflow_type_version and workflow_type_version != WORKFLOW_TYPE_VERSION:
        raise UnsupportedWorkflow(
            f"this server runs {WORKFLOW_TYPE} version "
            f"{WORKFLOW_TYPE_VERSION!r} and not {workflow_type_version!r}"
        )


def select_workflow(workflow_url, attachments):
    """Which attachment holds the workflow, and which are the run's inputs.

    WES submits a workflow either by URL or as an attachment named by a relative
    `workflow_url`. Only the second is supported: fetching a remote URL would
    make this service pull and execute a document from wherever a caller pointed
    it, which is the shape of the problem E1 exists to solve, and solving it
    twice by two different mechanisms is not better than solving it once.

    `attachments` is a sequence of (filename, path). Returns (workflow, inputs).
    """
    if not workflow_url:
        raise MissingWorkflow(
            "workflow_url is required and must name one of the "
            "workflow_attachment files"
        )
    if "://" in workflow_url:
        raise MissingWorkflow(
            f"workflow_url {workflow_url!r} is remote; this server only accepts "
            f"a workflow_url naming one of the workflow_attachment files"
        )

    wanted = workflow_url.lstrip("./")
    workflow = None
    inputs = []
    for filename, path in attachments:
        if filename == wanted and workflow is None:
            workflow = (filename, path)
        else:
            inputs.append((filename, path))
    if workflow is None:
        raise MissingWorkflow(
            f"workflow_url {workflow_url!r} does not name any of the "
            f"attachments ({', '.join(name for name, _ in attachments) or 'none'})"
        )
    return workflow, inputs


def service_info(state_counts, auth_provider=None):
    """What this server is and is not.

    `system_state_counts` is taken from the live store rather than remembered,
    because a count that drifts is worse than one that is expensive.
    """
    return {
        "id": "app.biochef.agent",
        "name": "BioChef Agent",
        "description": (
            "Executes BioChef workflow documents by converting them to "
            "Snakemake. Exposes the GA4GH WES surface over the same runs as "
            "its own API."
        ),
        "type": {"group": "org.ga4gh", "artifact": "wes", "version": WES_VERSION},
        "workflow_type_versions": {
            WORKFLOW_TYPE: {"workflow_type_version": [WORKFLOW_TYPE_VERSION]}
        },
        "supported_wes_versions": [WES_VERSION],
        # Attachments only. The service does not fetch inputs from anywhere the
        # caller names; see select_workflow.
        "supported_filesystem_protocols": ["file"],
        "workflow_engine_versions": {"snakemake": "9"},
        "default_workflow_engine_parameters": [],
        "system_state_counts": dict(state_counts),
        "auth_instructions_url": "",
        "tags": {
            "authentication": auth_provider or "unknown",
            "not_implemented": "; ".join(UNIMPLEMENTED),
        },
    }


def run_status(run):
    """The minimal shape: what a client polls."""
    return {"run_id": run.run_id, "state": run.state.value}


def run_log(run, request=None):
    """The full shape, populated only with what this service actually knows.

    Absent fields are absent rather than invented. A `run_log` carrying a
    fabricated command line or a zero exit code for a run that never finished
    would be worse than an empty object, because a client cannot tell a real
    zero from a placeholder one.
    """
    body = {
        "run_id": run.run_id,
        "request": request if request is not None else {},
        "state": run.state.value,
        "run_log": {"name": run.run_id},
        "task_logs": [],
        "outputs": run.outputs if run.outputs is not None else {},
    }

    step_status = getattr(run, "step_status", None) or {}
    for name in sorted(step_status):
        body["task_logs"].append({"name": name, "state": step_status[name]})

    if run.error is not None:
        # WES has nowhere for a failure reason on the run itself, so it goes
        # where a client will actually look for it.
        body["run_log"]["stderr"] = run.error
    return body


def run_list(runs):
    """WES's run listing.

    `next_page_token` is the empty string rather than absent: the specification
    types it as a string, and a client that pages until the token is empty
    should terminate rather than trip over a missing key. Pagination itself is
    not implemented, which service-info says.
    """
    return {"runs": [run_status(run) for run in runs], "next_page_token": ""}
