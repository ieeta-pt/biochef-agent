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
| `BIOCHEF_AUTH` | `none` | who may call it: `none` or `bearer` |
| `BIOCHEF_AUTH_TOKEN` | | the shared token, required when `BIOCHEF_AUTH=bearer` |
| `BIOCHEF_PASSPORT_ISSUER` | *(unset)* | the issuer whose passports are accepted, when `BIOCHEF_AUTH=passport` |
| `BIOCHEF_PASSPORT_AUDIENCE` | *(required)* | the audience a passport must name; `any` to accept tokens minted for other services |
| `BIOCHEF_PASSPORT_JWKS_URL` | *(discovered)* | override the issuer's published key set URL |
| `BIOCHEF_PASSPORT_USERINFO_URL` | *(discovered)* | override the issuer's published UserInfo endpoint |
| `BIOCHEF_PASSPORT_VISA_ISSUERS` | *(unset)* | comma-separated issuers whose visas count |
| `BIOCHEF_PASSPORT_REQUIRE_VISA` | *(unset)* | a visa type a caller must hold to be let in |
| `BIOCHEF_PASSPORT_REQUIRE_VISA_VALUE` | *(unset)* | the exact value that visa must carry |
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

## Passports

`BIOCHEF_AUTH=passport` accepts a GA4GH Passport instead of a shared secret, and
names the caller `<issuer>#<subject>` — with the issuer, because two brokers can
each have a subject `12345` and recording only the second half would merge two
people into one caller.

**The visas do not come from the access token.** The GA4GH AAI profile is
explicit that "access tokens MUST NOT contain GA4GH Claims directly": the token
is the credential this service presents to the broker's UserInfo endpoint to
fetch the passport. LS AAI works exactly this way, handing a passport-scoped
access token to a downstream service which calls back to obtain the visas.

**A visa is signed separately from the passport carrying it**, usually by
somebody else: a broker authenticates you, and a data controller independently
asserts what you may see. So verifying the access token tells you *nothing*
about its visas. Each one is verified on its own, against its own issuer's keys,
and only from issuers named in `BIOCHEF_PASSPORT_VISA_ISSUERS`.

Without that list the feature would be worse than absent — anyone could stand up
an issuer, mint themselves the very visa being required, and be admitted by a
service that believes it is checking credentials. Setting
`BIOCHEF_PASSPORT_REQUIRE_VISA` without it **refuses to start**.

A visa here is a condition of being let in, not an authorisation model. What a
named caller may then do is still undecided, deliberately: returning roles
nobody consults would be worse than the gap.

Visas from issuers not on the list are **ignored, not fatal** — a passport
legitimately carries visas about institutions this service knows nothing about,
and refusing the request because of one would make a caller's unrelated
affiliations break their access here.

A passport carrying more than 128 visas is refused outright rather than having
the first 128 examined, because a legitimate passport whose relevant visa sat
past a silent cut would be denied for a reason nobody could see.

Both the key set and the UserInfo endpoint are required to be on the **issuer's
own host**. They are read from a document and then trusted completely, and a
`jwks_uri` pointing elsewhere is a full authentication bypass, since keys fetched
from there validate tokens. A `userinfo_endpoint` pointing elsewhere sends every
caller's access token to whoever is listening. LS AAI serves both from its own
host; a deployment that genuinely splits them sets the endpoint explicitly.

Refusals do not say which check failed. Whether it was the signature, the
issuer, the audience or the expiry is a fact about this deployment's
configuration, and telling an unauthenticated caller is telling them how to aim.

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
