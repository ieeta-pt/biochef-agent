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

## How a request is served

1. Uploaded files are written into `tmp/`.
2. The workflow JSON is parsed, and each node's bundle is pulled from the
   registry and its binary copied in.
3. A `Snakefile` is generated: one rule per node, with the node's inputs,
   outputs and command line.
4. `snakemake` runs it.
5. Each declared output is read back and base64-encoded into the response.

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
