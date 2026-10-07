# biochef-agent

An execution endpoint that takes a BioChef workflow — or individual steps of one
— to where the data and the compute already are: a Trusted Research Environment,
an HPC cluster, an institutional server.

The editor builds a workflow as a graph of tool invocations, and much of it runs
as WebAssembly in the page. That works while the data is something the browser
may hold. Often it is not. Controlled-access data cannot leave the environment
that governs it, a cohort can be too large to ship, and some steps need
resources no tab has. In each case the answer is the same: send the computation
to the data rather than the data to the computation.

This service is the far end of that dispatch. It takes the same workflow
description the editor produces, fetches the tools from the registry, generates
a [Snakemake](https://snakemake.github.io/) workflow, runs it where it is
deployed, and returns the results.

The direction of travel is federated. The roadmap works towards GA4GH
interoperability — resolving DRS identifiers across sites, streaming htsget
slices as inputs, validating Passport visas to decide what a caller may run and
read, exposing the agent itself as a WES endpoint, and dispatching heavy steps
of a DAG to an institutional TES — and then towards silo mode, where an agent
runs against data that never leaves its site at all, under a policy declaring
what is eligible and what privacy budget an analysis may spend.

None of that is built yet. What exists today is the single endpoint below.

## The contract

`openapi.json` is generated from the service (`python ci/export_openapi.py`)
and checked by the suite, so the committed contract cannot drift.

Two ways to run the same workflow.

**Synchronously.** `POST /convert`, `multipart/form-data`, two fields. The
connection is held for the whole run — up to `BIOCHEF_RUN_TIMEOUT`, fifteen
minutes by default — and the outputs come back in the response. This is the
contract the editor speaks today.

**Asynchronously.** `POST /runs` takes the same two fields and answers `202`
immediately with a `run_id`. `GET /runs/{run_id}` reports the state, and carries
the outputs once it is `COMPLETE`. States use the eight WES-style names in issue #5 —
`QUEUED`, `INITIALIZING`, `RUNNING`, `COMPLETE`, `EXECUTOR_ERROR`,
`SYSTEM_ERROR`, `CANCELING`, `CANCELED`. A complete WES API is separate work.

`GET /runs/{run_id}` carries `steps` while the run is happening — one of
`PENDING`, `RUNNING`, `COMPLETE`, `FAILED` per node, which an editor can use to
colour nodes. Snakemake announces each job as it starts and finishes, and the
agent publishes those changes before the run ends for clients polling this
endpoint. A node nobody has mentioned is `PENDING`, which is
what snakemake implies by saying nothing about a job until it starts it.
These are the last states announced by the engine. Cancellation can leave a
step marked `RUNNING`; the run's terminal state takes precedence in the UI.

`GET /runs/{run_id}/outputs/{node}/{handle}` streams one output as raw bytes —
no base64, and never assembled in memory. This is how a file larger than memory
comes back: the encoded response above costs a second copy plus a third again
for the encoding, so a 4 GiB output would need roughly 14.7 GiB resident. A
client names a node and a handle, never a path.

Outputs stay fetchable for `BIOCHEF_KEEP_OUTPUTS` seconds and for the most
recent `BIOCHEF_MAX_RETAINED_RUNS` runs, whichever runs out first; after that the
endpoint answers `410 Gone` rather than pretending the run never existed.
Retention is bounded in both directions because "stop deleting" is how a service
fills a disk.

`GET /runs/{run_id}/manifest` returns how the run was produced: the workflow by
digest, each tool by the digests the registry stated for it, each input as
received, outputs by content, the runner and image, and the exit code. If a run
changes an input, `inputs_after` records that input's final digest (or `null` if
it was removed or could not be read); the field is omitted when inputs are
unchanged. It is also written into the run's own directory as `run.json`, beside
the outputs it describes.

The vocabulary is the hub's. It publishes `biochef.build-evidence.v1` alongside
each bundle and signs artifacts as in-toto statements, so a run manifest carries
that evidence forward by reference rather than restating the same facts in
different words. A bundle built before that work still produces a manifest —
provenance should not be a reason not to run something.

A run that **failed** gets one too, with its real exit code and every declared
output recorded by its final identity, including `null` for outputs that are
missing — that is the run whose exit code matters most. A run that has
nowhere to put a manifest gets none: `/convert` deletes its workspace on the way
out and has no run id to attach one to, and building it costs a second full read
of every input and output.

It records what was fixed. It does **not** promise reproducibility: a tool that
reads the clock, the network, or a file the manifest cannot name will not
reproduce, and that is a property of the tool rather than of this document.

`GET /runs/{run_id}/logs` returns Snakemake's run-wide `stdout` and `stderr`,
plus `node_logs`: separate stdout and stderr captured for each rule that ran.
A failing tool's own stderr is in its node's entry. Stdout that a recipe directs
to a scientific output file stays in that output and is not copied into logs.
`failed_steps` indexes Snakemake-style error headings in the run-wide stderr
(called `steps` in #67). It is a diagnostic hint, not proof of step identity; use `node_logs`
for what each rule printed. Rules share a workspace, so these files are
diagnostic records rather than tamper-proof audit evidence.

**The logs are not streamed, though progress is updated live.** The runner
drains both pipes as output arrives, using the engine's stderr announcements
for progress. Run-wide output is recorded and per-rule log files are read when
the workflow process exits. Logs therefore appear before the run reaches a
terminal state, but not while tools are running.
`/convert` keeps its existing shell command and failure response; per-rule
capture applies to asynchronous `/runs` only.

`POST /runs/{run_id}/cancel` stops one. A run still waiting for a slot has
executed nothing, so it settles `CANCELED` without ever starting; a run that is
executing has its process group ended — the tool and children that remain in
that group, using the same lever as the timeout. The reply is normally
`CANCELING` while the worker tidies up, but may already be `CANCELED` if it
finishes before the response. A cancelled run returns no outputs even if the
work finished anyway.

Runs are held in memory: nothing survives a restart, and nothing is shared
between replicas. `BIOCHEF_MAX_RUNS` bounds how many are remembered; finished
runs are evicted first, while a full set of active runs refuses new work with
HTTP 503. A rejected request has no `run_id`; the caller can retry later.
`/convert` and `/runs` share the same execution slots, though `/convert` still
holds its HTTP connection open. Queued `/runs` jobs still hold their uploaded
inputs in memory; the request-size limit is not an aggregate memory limit.

**Budget for that.** At the defaults the two retained run-wide stream tails can
reach 512 MiB — `BIOCHEF_MAX_RUNS` × `BIOCHEF_MAX_LOG_BYTES` × two streams.
The per-node stdout and stderr tails share another two-stream budget per run,
adding up to another 512 MiB at the defaults. A run also holds diagnostic
error blocks and base64-encoded outputs. The runner still buffers complete
run-wide streams before truncation, and per-rule log files can grow on disk
until the run ends. This setting is a retention bound, not a peak resource
limit. Logs may contain sensitive tool output; this prototype has no local
log-release policy for TRE use.

Both take the same fields:

| field | what it is |
|---|---|
| `biochef_workflow` | the editor's workflow JSON, as a string: `{"nodes": [...], "edges": [...]}` |
| `files` | the input files, one part each |

Inputs go through a `DataSource`. Request uploads use `spooled` for `/convert`
and `handedover` for `/runs`; `upload` remains available for inputs already held
as bytes. `localpath` lets a workflow name a file already on the agent's host —
see `BIOCHEF_DATA_SOURCES` below — and a provider writes into the run's workspace
rather than returning bytes, so a large input is streamed rather than held whole.

**Uploaded files must be named for the edge that carries them.** The converter
names every intermediate file `{source_node_id}-{source_handle}`, so a file
feeding the `out` handle of node `input-1` must be uploaded as `input-1-out`.
A file whose name does not match an input the workflow expects is not an error —
it is simply never read, and the run fails later looking for something that is
not there.

The response is a JSON object keyed by node id, then by output handle, with each
value base64-encoded:

```json
{
  "tn93.distance-1": {
    "out": "MC4wMSAwLjAyCg=="
  }
}
```

### What the workflow JSON has to contain

Only three things are read from each node:

- `id` — used for the Snakemake rule name and for naming intermediate files
- `data.repo` — the registry path the tool bundle is pulled from
- `data.paramValues` — `{name: {enabled: bool, value: any}}`; a parameter is
  emitted only when `enabled` is exactly `true`

Everything else about the tool — its binary, its inputs and outputs, its
parameter flags — comes from the bundle fetched from the registry, not from the
request.

## Running it

```
./run.sh
```

which creates a virtualenv, installs `requirements.txt`, and starts the service
on the FastAPI default port. `snakemake` is one of the pinned requirements, so
nothing else needs installing.

Configuration is by environment variable, and `example.env` lists them:

| variable | default | what it does |
|---|---|---|
| `REGISTRY_URL` | `registry.biochef.app` | where tool bundles are pulled from |
| `REGISTRY_USERNAME` | | registry credentials |
| `REGISTRY_PASSWORD` | | |
| `REGISTRY_INSECURE` | `false` | allow a plain-HTTP registry |
| `ORAS_AUTH_BACKEND` | `token` | ORAS authentication backend |
| `BIOCHEF_TOOL_CACHE` | `tool-cache` | where pulled tool bundles are kept between runs |
| `BIOCHEF_DATA_SOURCES` | `upload,spooled,handedover` | permitted input providers; add `localpath` to allow reads under `BIOCHEF_LOCAL_ROOT` |
| `BIOCHEF_LOCAL_ROOT` | | the only directory `localpath` may read from |
| `BIOCHEF_RUN_ROOT` | the system temp directory | where a run's private directory is created |
| `BIOCHEF_RUN_TIMEOUT` | `900` | seconds before a run's whole process group is killed |
| `BIOCHEF_KEEP_WORKSPACE` | `false` | leave a run's directory behind, for debugging |
| `BIOCHEF_MAX_UPLOAD_BYTES` | `536870912` | largest request body accepted, in bytes |
| `BIOCHEF_MAX_RUNS` | `256` | maximum run records retained for polling |
| `BIOCHEF_MAX_LOG_BYTES` | `1048576` | retained bytes per run-wide stream and shared budget per node-log stream, tail first |
| `BIOCHEF_KEEP_OUTPUTS` | `3600` | seconds a finished run's outputs stay fetchable; `0` deletes them at once |
| `BIOCHEF_MAX_RETAINED_RUNS` | `32` | how many finished runs may keep outputs on disk |
| `BIOCHEF_MAX_CONCURRENT_RUNS` | `4` | execution slots shared by `/runs` and `/convert`; admitted `/runs` jobs wait in `QUEUED` |
| `BIOCHEF_AUTH` | `none` | who may call it: `none` or `bearer` |
| `BIOCHEF_AUTH_TOKEN` | | the shared token, required when `BIOCHEF_AUTH=bearer` |
| `BIOCHEF_RUNNER` | `subprocess` | how a workflow executes: `subprocess` or `apptainer` |
| `BIOCHEF_CONTAINER_IMAGE` | `docker://debian:stable-slim` | image each step runs in, under the `apptainer` runner |
| `BIOCHEF_APPTAINER_CACHE` | `apptainer-cache` | where pulled container images are kept between runs |
| `BIOCHEF_APPTAINER_ARGS` | `--contain --cleanenv` | extra flags for apptainer itself |
| `BIOCHEF_SIGNING_MODE` | `off` | `off`, `warn`, or `strict` — whether a bundle must be signed to run |
| `BIOCHEF_SIGNING_POLICY` | *(unset)* | path to the Hub's `biochef.signing-policy.v1` document |
| `BIOCHEF_COSIGN` | `cosign` | the cosign executable to verify with |
| `BIOCHEF_SLSA_VERIFIER` | `slsa-verifier` | the SLSA Verifier executable to verify official provenance with |

`BIOCHEF_AUTH` defaults to `none`, which means **any caller that can open a
socket to this service can make it execute tool binaries**. That is a reasonable
default on a laptop and the wrong one anywhere else. `bearer` requires
`Authorization: Bearer <token>` matching `BIOCHEF_AUTH_TOKEN`; a shared secret is
not identity -- every holder is the same caller -- but it is the difference
between an open endpoint and a closed one. Selecting `bearer` without a token
stops the service from starting rather than letting it run with a token nobody
has to guess.

`BIOCHEF_DATA_SOURCES` decides which input providers are enabled. It defaults to
`upload,spooled,handedover`: direct bytes, the synchronous route's request spool,
and the asynchronous route's Agent-owned temporary copy. Adding `localpath` lets
a workflow name a file already on the agent's host, which is the ordinary case
inside a TRE where the data is already on the machine.
Existing `upload`-only configurations need `spooled` and `handedover` to use
these HTTP routes.
With an `upload`-only configuration, a submitted `/runs` workflow reaches `EXECUTOR_ERROR` because `handedover` is disabled, and an output download returns HTTP 409. Check `GET /runs/{run_id}` for the underlying error.

Raw output downloads from `/runs/{run_id}/outputs/{node_id}/{handle}` are
available only after the run reaches `COMPLETE`; other states return HTTP 409.
Status responses still include base64 outputs.

**`localpath` requires `BIOCHEF_LOCAL_ROOT` to be an existing directory,** and
refuses to start without it.
The client chooses the path, so a source that could read anywhere would be an
arbitrary-file-read with a workflow engine attached: a workflow naming
`/etc/shadow` as an input would have it copied into a workspace and returned as a
tool's output. Paths are resolved before being checked against the root, so a
symlink inside it pointing outward is refused too.

The root and its parent directories must not be modifiable by untrusted users
or tools: validation and opening are separate operations. This provider does
not enforce filesystem permissions or prevent changes between those operations.

Three more decide how isolated a run is, and are worth reading twice before
changing.

`BIOCHEF_RUNNER` defaults to `subprocess`, which runs every step **on the host,
as the user this service runs as**. `apptainer` runs each step in a container
instead. The container is the boundary between an untrusted tool binary and the
machine, so on any deployment holding data that matters, `apptainer` is the
setting you want.

`BIOCHEF_SIGNING_MODE` defaults to `off`, which is what this service did before
it could verify anything and is named so that it reads as a decision. The Hub
signs and attests every bundle it publishes; `strict` makes a bundle that does
not carry a signature this policy accepts refuse to run, which is what a TRE
needs. `warn` verifies and reports but runs anyway, which is how you find out
what your catalogue actually looks like before switching it on.

`strict` fails closed in every direction: no cosign on `PATH`, no policy, an
unreadable policy, a reference outside the policy's own registry prefix, or a
manifest whose digest could not be established are all refusals. A verification
step that passes when it could not run is worse than none, because it is
believed.

In `strict`, the caller must provide an immutable digest, and the Agent also
checks the CycloneDX and SLSA evidence against the files it executes.
Cosign and SLSA Verifier must be installed in the Agent's environment; the Agent
does not download them at runtime. `warn` reports failed checks and
continues, while `off` preserves the previous local-development behaviour.

`BIOCHEF_CONTAINER_IMAGE` must carry a scheme — `docker://`, `oras://`,
`library://`, `shub://`, `http://`, `https://` — or be an absolute path to a
`.sif`. A value without one is not a registry reference to snakemake; it is a
local image *file*, resolved against the run's own directory. The service
refuses to start rather than let a typo mean that. The image also has to contain
`bash`, because snakemake runs each rule as `bash -c` inside it.

**Pin it by digest on anything long-lived.** The default is a moving tag, and
snakemake caches a pulled image under the md5 of the *reference string* and
skips the pull whenever that file already exists. So `docker://debian:stable-slim`
is fetched once and then never revalidated: the tag moves, the cached image does
not, and base-image security updates never arrive. A digest —
`docker://debian@sha256:…` — makes the reference change when the image does,
which is the only way that cache invalidates. Deleting `apptainer-cache/` forces
a re-pull in the meantime.

`BIOCHEF_APPTAINER_ARGS` defaults to `--contain --cleanenv`. Without `--contain`, apptainer binds the host's `/tmp` into the container, and that is where a run's directory lives unless `BIOCHEF_RUN_ROOT` says otherwise. Without it, a containerised tool is walled off from `/usr` and `/etc` while still able to read **every other run's data**. Without `--cleanenv`, the tool process also inherits the Agent's environment. Emptying this variable turns both protections off deliberately.

## How a request is served

1. A private directory is created for this run, and uploaded files are written
   into it — only those the workflow declares as inputs.
2. The workflow JSON is parsed, and each node's bundle is pulled from the
   registry and its binary copied in.
3. A `Snakefile` is generated: one rule per node, with the node's inputs,
   outputs and command line.
4. The configured runner executes it, bounded by `BIOCHEF_RUN_TIMEOUT`, and the
   whole process group is killed if it overruns.
5. Each declared output is read back and base64-encoded into the response.
6. The run's directory is removed, whether it succeeded or not.

## Before deploying this

**It is not ready to be exposed.** Several open issues describe defects reachable
by anyone who can reach the port. The most significant are tracked in the issue
tracker; read them before putting this anywhere a stranger can send it a request.

Authentication now exists but is **off by default**. `BIOCHEF_AUTH=none` is the
default, and it means what it says: any caller that can open a socket can make
this service execute tool binaries. Setting `BIOCHEF_AUTH=bearer` closes that,
and a deployment that does not is choosing to leave it open.

Even set, a shared token is not identity — every holder is the same caller, and
nothing yet decides what a given caller may run or read. That is F3 (Passports),
and it does not exist.

That matters more here than the sentence usually implies. The environments this
is aimed at are the ones where it would do the most damage: an agent inside a
TRE sits next to data that is there precisely because it may not leave.

Development is organised as numbered workstreams (A–G) in the issues: the
converter and its intermediate model, asynchronous runs, authentication and
execution hygiene, data sources, supply chain, GA4GH interoperability, and
federation. Each issue states what to verify first, what to deliver, and how to
know it is done.

The conventions those issues set, which any change here should follow:

- one PR implements one interface or one provider, never both
- the first commit of a PR is the verification step, and the description says
  what was found before anything was changed
- a PR touching a contract updates the contract file in the same PR
- new functionality lands behind a feature flag
- every PR adds or updates tests for what it touches

## Related repositories

| repository | what it holds |
|---|---|
| `Biochef` | the editor, which produces the workflow JSON this service consumes |
| `biochef-recipes` | one `biochef.yaml` per tool, declaring its operations and IO |
| `biochef-hub` | validates recipes, builds them, and publishes bundles to the registry |
