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

`openapi.json` in this repository is the source of truth, generated from the
service itself (`python ci/export_openapi.py`) and checked by the suite, so it
cannot drift.

Two ways to run the same workflow.

**Synchronously.** `POST /convert`, `multipart/form-data`, two fields. The
connection is held for the whole run — up to `BIOCHEF_RUN_TIMEOUT`, fifteen
minutes by default — and the outputs come back in the response. This is the
contract the editor speaks today.

**Asynchronously.** `POST /runs` takes the same two fields and answers `202`
immediately with a `run_id`. `GET /runs/{run_id}` reports the state, and carries
the outputs once it is `COMPLETE`. States are GA4GH WES's vocabulary verbatim —
`QUEUED`, `INITIALIZING`, `RUNNING`, `COMPLETE`, `EXECUTOR_ERROR`,
`SYSTEM_ERROR`, `CANCELING`, `CANCELED` — so exposing this as a WES endpoint
later is an adapter rather than a rewrite.

`GET /runs/{run_id}` carries `steps` while the run is happening — one of
`PENDING`, `RUNNING`, `COMPLETE`, `FAILED` per node, which is what the editor
paints. Snakemake announces each job as it starts and finishes, and the agent
reads its output as it arrives rather than at the end, so a node changes colour
while the work is going on. A node nobody has mentioned is `PENDING`, which is
what snakemake implies by saying nothing about a job until it starts it.

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
digest, each tool by the digests the registry stated for it, every input and
output by content, the runner and image, and the exit code. It is also written
into the run's own directory as `run.json`, beside the outputs it describes.

The vocabulary is the hub's. It publishes `biochef.build-evidence.v1` alongside
each bundle and signs artifacts as in-toto statements, so a run manifest carries
that evidence forward by reference rather than restating the same facts in
different words. A bundle built before that work still produces a manifest —
provenance should not be a reason not to run something.

A run that **failed** gets one too, with its real exit code and its outputs
recorded as absent — that is the run whose exit code matters most. A run that has
nowhere to put a manifest gets none: `/convert` deletes its workspace on the way
out and has no run id to attach one to, and building it costs a second full read
of every input and output.

It records what was fixed. It does **not** promise reproducibility: a tool that
reads the clock, the network, or a file the manifest cannot name will not
reproduce, and that is a property of the tool rather than of this document.

`GET /runs/{run_id}/logs` returns what the run printed, plus `failed_steps`,
naming the nodes that failed and what snakemake said about each.

**The logs are not streamed, though the progress is.** They are recorded in one
go when the workflow process exits — readable while the run is still finishing,
but not during it. The two differ because progress is a handful of state
transitions and the logs are unbounded output; flushing every line into the run
store would take a lock per line. Following the output live is a separate piece
of work. A step that succeeded is not separated out:
its output is in `stdout` along with everything else's, and nothing in
snakemake's output marks where one rule's writing ends. Splitting that needs a
`log:` directive per rule, which is emitter work.

`POST /runs/{run_id}/cancel` stops one. A run still waiting for a slot has
executed nothing, so it settles `CANCELED` without ever starting; a run that is
executing has its whole process group ended — the tool and anything it spawned,
the same lever the timeout pulls. The reply is `CANCELING`, because the kill has
been issued but the run is not over until the worker has tidied up and said so.
A cancelled run returns no outputs even if the work finished anyway.

Runs are held in memory: nothing survives a restart, and nothing is shared
between replicas. `BIOCHEF_MAX_RUNS` bounds how many are remembered, and a run
still in flight is never forgotten.

**Budget for that.** At the defaults the logs alone can reach 512 MiB —
`BIOCHEF_MAX_RUNS` × `BIOCHEF_MAX_LOG_BYTES` × two streams — and each remembered
run also holds its outputs, base64-encoded, bounded only by how many runs are
kept. A deployment that returns large outputs should lower `BIOCHEF_MAX_RUNS`,
`BIOCHEF_MAX_LOG_BYTES`, or both.

Both take the same fields:

| field | what it is |
|---|---|
| `biochef_workflow` | the editor's workflow JSON, as a string: `{"nodes": [...], "edges": [...]}` |
| `files` | the input files, one part each |

Inputs go through a `DataSource`. `upload` is the default and the only one
enabled out of the box, so the request above is unchanged. `localpath` lets a
workflow name a file already on the agent's host — see `BIOCHEF_DATA_SOURCES`
below — and a provider writes into the run's workspace rather than returning
bytes, so a large input is streamed rather than held whole.

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
| `BIOCHEF_DATA_SOURCES` | `upload` | where inputs may come from: `upload`, `localpath` |
| `BIOCHEF_LOCAL_ROOT` | | the only directory `localpath` may read from |
| `BIOCHEF_RUN_ROOT` | the system temp directory | where a run's private directory is created |
| `BIOCHEF_RUN_TIMEOUT` | `900` | seconds before a run's whole process group is killed |
| `BIOCHEF_KEEP_WORKSPACE` | `false` | leave a run's directory behind, for debugging |
| `BIOCHEF_MAX_UPLOAD_BYTES` | `536870912` | largest request body accepted, in bytes |
| `BIOCHEF_MAX_RUNS` | `256` | how many runs are remembered for polling |
| `BIOCHEF_MAX_LOG_BYTES` | `1048576` | how much of a run's output is kept, tail first |
| `BIOCHEF_KEEP_OUTPUTS` | `3600` | seconds a finished run's outputs stay fetchable; `0` deletes them at once |
| `BIOCHEF_MAX_RETAINED_RUNS` | `32` | how many finished runs may keep outputs on disk |
| `BIOCHEF_MAX_CONCURRENT_RUNS` | `4` | how many runs execute at once; the rest wait in `QUEUED` |
| `BIOCHEF_AUTH` | `none` | who may call it: `none` or `bearer` |
| `BIOCHEF_AUTH_TOKEN` | | the shared token, required when `BIOCHEF_AUTH=bearer` |
| `BIOCHEF_RUNNER` | `subprocess` | how a workflow executes: `subprocess` or `apptainer` |
| `BIOCHEF_CONTAINER_IMAGE` | `docker://debian:stable-slim` | image each step runs in, under the `apptainer` runner |
| `BIOCHEF_APPTAINER_CACHE` | `apptainer-cache` | where pulled container images are kept between runs |
| `BIOCHEF_APPTAINER_ARGS` | `--contain` | extra flags for apptainer itself |

`BIOCHEF_AUTH` defaults to `none`, which means **any caller that can open a
socket to this service can make it execute tool binaries**. That is a reasonable
default on a laptop and the wrong one anywhere else. `bearer` requires
`Authorization: Bearer <token>` matching `BIOCHEF_AUTH_TOKEN`; a shared secret is
not identity -- every holder is the same caller -- but it is the difference
between an open endpoint and a closed one. Selecting `bearer` without a token
stops the service from starting rather than letting it run with a token nobody
has to guess.

`BIOCHEF_DATA_SOURCES` decides where a run's inputs may come from. It defaults
to `upload` alone — bytes pushed in the request, which is what the editor does.
Adding `localpath` lets a workflow name a file already on the agent's host, which
is the ordinary case inside a TRE where the data is already on the machine.

**`localpath` requires `BIOCHEF_LOCAL_ROOT`,** and refuses to start without it.
The client chooses the path, so a source that could read anywhere would be an
arbitrary-file-read with a workflow engine attached: a workflow naming
`/etc/shadow` as an input would have it copied into a workspace and returned as a
tool's output. Paths are resolved before being checked against the root, so a
symlink inside it pointing outward is refused too.

Three more decide how isolated a run is, and are worth reading twice before
changing.

`BIOCHEF_RUNNER` defaults to `subprocess`, which runs every step **on the host,
as the user this service runs as**. `apptainer` runs each step in a container
instead. The container is the boundary between an untrusted tool binary and the
machine, so on any deployment holding data that matters, `apptainer` is the
setting you want.

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

`BIOCHEF_APPTAINER_ARGS` defaults to `--contain` because apptainer otherwise
binds the host's `/tmp` into the container, and that is where a run's directory
lives unless `BIOCHEF_RUN_ROOT` says otherwise. Without it, a containerised tool
is walled off from `/usr` and `/etc` while still able to read **every other
run's data**. Emptying this variable turns that off deliberately.

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
