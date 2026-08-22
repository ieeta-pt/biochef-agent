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

One endpoint. `POST /convert`, `multipart/form-data`, two fields:

| field | what it is |
|---|---|
| `biochef_workflow` | the editor's workflow JSON, as a string: `{"nodes": [...], "edges": [...]}` |
| `files` | the input files, one part each |

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
| `BIOCHEF_RUN_ROOT` | the system temp directory | where a run's private directory is created |
| `BIOCHEF_RUN_TIMEOUT` | `900` | seconds before a run's whole process group is killed |
| `BIOCHEF_KEEP_WORKSPACE` | `false` | leave a run's directory behind, for debugging |
| `BIOCHEF_MAX_UPLOAD_BYTES` | `536870912` | largest request body accepted, in bytes |
| `BIOCHEF_RUNNER` | `subprocess` | how a workflow executes: `subprocess` or `apptainer` |
| `BIOCHEF_CONTAINER_IMAGE` | `docker://debian:stable-slim` | image each step runs in, under the `apptainer` runner |
| `BIOCHEF_APPTAINER_CACHE` | `apptainer-cache` | where pulled container images are kept between runs |
| `BIOCHEF_APPTAINER_ARGS` | `--contain` | extra flags for apptainer itself |

Three of those decide how isolated a run is, and are worth reading twice before
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

**It is not ready to be exposed.** There is no authentication of any kind, and
several open issues describe defects reachable by anyone who can reach the port.
The most significant are tracked in the issue tracker; read them before putting
this anywhere a stranger can send it a request.

That matters more here than the sentence usually implies. The environments this
is aimed at are the ones where it would do the most damage: an agent inside a
TRE sits next to data that is there precisely because it may not leave. Deciding
what a caller may run and read is C2 and F3, and neither exists yet.

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
