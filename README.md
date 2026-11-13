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

**Run-wide output is live; per-rule logs are not.** `stdout` and `stderr` are
read as they arrive and handed to the run twice a second, so a run that is still
going can be read as it goes. A tool that prints once and then works silently is
still visible: the flush is on a timer, because checking
the clock only when a line arrives would show nothing for as long as the tool
said nothing. The per-rule `node_logs` are different — they are read from each
rule's own file when the workflow process exits, so a mid-run poll carries
run-wide output and no node logs yet.

What a poll returns mid-run is therefore a **partial** log. The runner, live
buffer and run store each retain at most `BIOCHEF_MAX_LOG_BYTES` of text per
stream, plus a truncation marker. Oldest output is dropped first. The final
record is written when the process exits, from the runner's retained tails;
output already discarded by the runner is not recovered.

`failed_steps` is only derived once the run has an exit code, and only for a
failed one: a successful tool can print something that looks like a
snakemake error heading, so a live flush deliberately reports an exit code of
zero and lets nothing be parsed out of a run still in progress.

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
error blocks and base64-encoded outputs. The runner still reads a complete line
before trimming its tail, and per-rule log files can grow on disk
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
| `BIOCHEF_AUDIT_LOG` | *(unset)* | path to an append-only JSONL audit trail; unset writes none. Required by the [UI](#the-page-for-a-nodes-data-manager) |
| `BIOCHEF_DB_URL` | `sqlite:////var/lib/biochef/agent.db` | the node's database, see [The node's store](#the-nodes-store) |
| `BIOCHEF_NODE_SIGNING_KEY` | | path of the node's Ed25519 private key, see [The node's keys](#the-nodes-keys) |
| `BIOCHEF_NODE_ENCRYPTION_KEY` | | path of the node's X25519 private key |
| `BIOCHEF_NODE_NAME` | | this node's name, required by the UI |
| `BIOCHEF_FEDERATION_KEY` | | path of the PEM public key that signs the site list |
| `BIOCHEF_COORDINATOR_POLL_SECONDS` | `30` | how often the coordinator is polled; three intervals with no success shows it as offline |
| `BIOCHEF_EGRESS_MODE` | | `airlock` or `manual`, required by the UI, and what makes an agent a node, see [Egress](#egress) |
| `BIOCHEF_DATASET_BINDINGS_FILE` | | YAML file of dataset bindings, locked on the page |
| `BIOCHEF_DATASET_BINDINGS_FROM_UI` | `false` | allow bindings to be made on the page |
| `BIOCHEF_LS_LOGIN_ISSUER` | | LS Login issuer URL, for checking members' ID tokens |
| `BIOCHEF_EDITOR_CLIENT_ID` | | the editor's LS Login client id, the audience of those tokens |
| `BIOCHEF_UI_HOST` | `127.0.0.1` | interface the UI binds to |
| `BIOCHEF_UI_PORT` | `8081` | UI port |
| `BIOCHEF_UI_IDENTITY` | | `header` or `dev`, no default, see [The page for a node's data manager](#the-page-for-a-nodes-data-manager) |
| `BIOCHEF_UI_IDENTITY_HEADER` | | header carrying the user name, required with `header` |
| `BIOCHEF_UI_TRUSTED_PROXIES` | | comma separated CIDRs allowed to set that header, required with `header` |
| `BIOCHEF_UI_DEV_USER` | | fixed user, required with `dev` |
| `BIOCHEF_POLICY_ENGINE_URL` | | where "Open Policy Engine" points; unset hides the link |
| `BIOCHEF_KEEP_WORKSPACE` | `false` | leave a run's directory behind, for debugging |
| `BIOCHEF_MAX_UPLOAD_BYTES` | `536870912` | largest request body accepted, in bytes |
| `BIOCHEF_MAX_RUNS` | `256` | maximum run records retained for polling |
| `BIOCHEF_MAX_LOG_BYTES` | `1048576` | retained bytes per run-wide stream and shared budget per node-log stream, tail first |
| `BIOCHEF_KEEP_OUTPUTS` | `3600` | seconds a finished run's outputs stay fetchable; `0` deletes them at once |
| `BIOCHEF_MAX_RETAINED_RUNS` | `32` | how many finished runs may keep outputs on disk |
| `BIOCHEF_MAX_CONCURRENT_RUNS` | `4` | execution slots shared by `/runs` and `/convert`; admitted `/runs` jobs wait in `QUEUED` |
| `BIOCHEF_AUTH` | `none` | who may call it: `none` or `bearer` |
| `BIOCHEF_AUTH_TOKEN` | | the shared token, required when `BIOCHEF_AUTH=bearer` |
| `BIOCHEF_PASSPORT_ISSUER` | *(unset)* | the issuer whose passports are accepted, when `BIOCHEF_AUTH=passport` |
| `BIOCHEF_PASSPORT_AUDIENCE` | *(required)* | the audience a passport must name; `any` to accept tokens minted for other services |
| `BIOCHEF_PASSPORT_JWKS_URL` | *(discovered)* | override the issuer's published key set URL |
| `BIOCHEF_PASSPORT_USERINFO_URL` | *(discovered)* | override the issuer's published UserInfo endpoint |
| `BIOCHEF_PASSPORT_VISA_ISSUERS` | *(unset)* | comma-separated issuers whose visas count |
| `BIOCHEF_PASSPORT_REQUIRE_VISA` | *(unset)* | a visa type a caller must hold to be let in |
| `BIOCHEF_PASSPORT_REQUIRE_VISA_VALUE` | *(unset)* | the exact value that visa must carry |
| `BIOCHEF_RUNNER` | `subprocess` | how a workflow executes: `subprocess`, `apptainer`, `singularity`, `podman` or `tes` |
| `BIOCHEF_REQUIRE_NETWORK_ISOLATION` | `true` | refuses to start with a runner the agent does not itself keep off the network; `false` turns that off and is audited at start, see [What each runner guarantees](#what-each-runner-guarantees) |
| `BIOCHEF_NETWORK_PROBE` | *(unset)* | `host:port` the agent can reach and a step must not, tried by the network check at start |
| `BIOCHEF_PODMAN_MODE` | | `rootless` or `rootful`, required with `podman`, see [Running it](#running-it) |
| `BIOCHEF_PODMAN_SELINUX` | | `disable` or `keep`, required with rootless `podman` |
| `BIOCHEF_STEP_PIDS` | | the most processes a step may have, required with rootless `podman` |
| `BIOCHEF_STEP_MEMORY` | | the most memory a step may use, such as `4g`, with no swap past it, required with rootless `podman` |
| `BIOCHEF_PODMAN_HELPER` | | the absolute path of the installed `podman-step-root`, required with rootful `podman` |
| `BIOCHEF_TES_URL` | | base URL of the TES server, required with `tes`: https, or http to this host only |
| `BIOCHEF_TES_IMAGE` | | the image every step runs in, by digest (`repository@sha256:<64 hex>`), required with `tes` |
| `BIOCHEF_TES_AUTH` | | `bearer`, `basic` or `none`, required with `tes` |
| `BIOCHEF_TES_TOKEN_FILE` | | the absolute path of a file (mode 0600) holding the bearer token, required with `bearer` and refused with the others |
| `BIOCHEF_TES_USER` | | the user of the server's basic authentication, required with `basic`: letters, digits and `. _ @ -`, no colon |
| `BIOCHEF_TES_PASSWORD_FILE` | | the absolute path of a file (mode 0600) holding that user's password, required with `basic` |
| `BIOCHEF_TES_CA` | | the absolute path of a CA bundle for the server's certificate, optional |
| `BIOCHEF_TES_STAGING` | | a directory of the agent's, which the server can read and write, required with `tes` |
| `BIOCHEF_TES_HELPER` | | the absolute path of the installed `tes-step-root`, required with `tes` |
| `BIOCHEF_TES_ISOLATION` | | `attested` or `none`, required with `tes`: the operator's statement about the server, see [The TES runner](#the-tes-runner) |
| `BIOCHEF_TESAPI_TOKENS` | | the absolute path of a file (mode 0600) of `name sha256:<hex>` lines, one per caller of the TES API; required by `python -m tesapi` |
| `BIOCHEF_TESAPI_IMAGES` | | comma-separated images by digest a TES task may name; required by `python -m tesapi` |
| `BIOCHEF_TESAPI_READ_DIRS` | | comma-separated absolute directories a TES task may read its inputs from, through `file://`; required by `python -m tesapi` |
| `BIOCHEF_TESAPI_WRITE_DIRS` | | comma-separated absolute directories a TES task may write its outputs to; required, and may share no directory with `BIOCHEF_TESAPI_READ_DIRS`, or the listener does not start |
| `BIOCHEF_TESAPI_DIRS` | | removed. If set, `python -m tesapi` refuses to start and says to use the two above |
| `BIOCHEF_TESAPI_HOST` | `127.0.0.1` | the interface the TES API listens on; anything else needs TLS |
| `BIOCHEF_TESAPI_PORT` | `8082` | the TES API's port |
| `BIOCHEF_TESAPI_TLS_CERT`, `BIOCHEF_TESAPI_TLS_KEY` | | certificate and key files, both or neither, required off this host |
| `BIOCHEF_TESAPI_MAX_CONCURRENT` | `1` | how many TES tasks run at once, apart from the agent's own runs |
| `BIOCHEF_TESAPI_MAX_QUEUED` | `16` | how many wait for a slot; another gets 503 |
| `BIOCHEF_TESAPI_MAX_BYTES` | `1073741824` | the most a task may copy in and out together |
| `BIOCHEF_TESAPI_TIMEOUT` | `900` | seconds a task may run |
| `BIOCHEF_CONTAINER_IMAGE` | `docker://debian:stable-slim` | image each step runs in, under the `apptainer` runner |
| `BIOCHEF_APPTAINER_BINARY` | | the absolute path of the apptainer or SingularityCE binary, required with `apptainer` and `singularity` |
| `BIOCHEF_APPTAINER_CACHE` | `apptainer-cache` | where pulled container images are kept between runs |
| `BIOCHEF_APPTAINER_ARGS` | `--contain --cleanenv --net --network none` | extra flags for apptainer itself |
| `BIOCHEF_TOOL_CATALOG` | `required` | `required` or `off`: whether every tool is resolved through the signed catalog |
| `BIOCHEF_CATALOG_PUBLIC_JWK` | *(the web app's key)* | the catalog's EC P-256 public key, as JSON, for a registry with its own catalog key |
| `BIOCHEF_CATALOG_PACKAGE` | `biochef-plugins-index` | the repository the catalog is published under |
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

**The audit trail records a caller only when the provider says its return names
one.** Every provider has to declare that, and one that does not stops the
service from starting — alongside the empty token and the unknown provider
name. There is deliberately no default: `bearer` returns a marker every holder
of the secret presents, and writing that into the trail's `caller` field would
read as a name to whoever opens the log years later; but a provider added later
that *does* carry identity and merely forgot to declare would have its callers
silently dropped instead, which is the worse of the two and the one nothing
would notice. So "we do not know who" stays a recorded fact about the
deployment rather than a gap in the log.

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

`BIOCHEF_TOOL_CATALOG` defaults to `required`: a workflow names a package, and
the Hub's signed catalog decides which manifest that is. The catalog is fetched
from the registry, its ES256 signature checked against the same key the web app
pins, and every reference resolved to the catalog's digest before anything is
fetched. A bare package name, `latest` or the catalog's version all resolve to
it. Any other tag, a digest the catalog does not list, or a package it does not
contain is refused. The exception is `strict` signing: an exact digest of a
package the catalog lists runs if its own signature, SBOM and provenance verify,
so that a workflow pinned to an older version survives a republish. A version
dropped from the catalog stays runnable that way while its signature is valid.
A catalog older than one this service has already accepted is refused too, and
the newest sequence is recorded next to the tool cache. A tag is a name anyone who
can write to the registry can move; the catalog is not. `off` sends references
to the registry as written, for a development registry that has no catalog.

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

In `strict`, the reference must be an immutable digest. The catalog supplies
it; with the catalog `off`, the caller must. The Agent also
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

`BIOCHEF_RUNNER=podman` runs each step with podman instead, through the `podman-step` script, which Snakemake uses as the shell for its rules. It needs a `docker://` image. Its steps have `--network=none`, a fixed choice with no setting, no capabilities, `no-new-privileges`, a read-only root filesystem with a fresh `/tmp`, limits on processes and memory with no swap past the memory limit, and only the run's workspace mounted. A container that a killed run leaves behind is removed afterwards. The agent refuses to start as root with this runner.

`BIOCHEF_PODMAN_MODE` must be set, with no default:

- `rootless` runs podman as the agent's user, and each step keeps that user. `BIOCHEF_PODMAN_SELINUX`, `BIOCHEF_STEP_PIDS` and `BIOCHEF_STEP_MEMORY` are required.
- `rootful` is for a host where podman runs as root, such as Debian 11's podman 3.0.1. Under root, `--userns=keep-id` does not keep the agent's user: it becomes the host's user namespace with uid 0, so a step would run as root on the host. The agent still does not run as root. It calls `podman-step-root` through one sudo rule, and each step runs as a dedicated user, never root and never the agent's own.

For `rootful`:

1. Make a group for the steps and put the agent's user in it, and pick a user id for the steps that is neither root nor the agent's. It needs no account.
2. Install `podman-step-root` owned by root, in a directory owned by root, neither writable by anyone else:
   `install -o root -g root -m 0755 podman-step-root /usr/local/libexec/biochef-agent/podman-step-root`
3. Write `/etc/biochef-agent/podman-step.conf`, owned by root and writable only by root, in a directory that is too. The path is fixed in the script. Every key is required, and an unknown or repeated one stops it:

   ```
   RUN_ROOT=/var/lib/biochef/runs
   STEP_UID=1601
   STEP_GID=1600
   IMAGE=docker://debian:stable-slim
   PIDS=256
   MEMORY=4g
   SELINUX=disable
   ```

   `RUN_ROOT` must be the agent's `BIOCHEF_RUN_ROOT`, a directory the agent owns, and `IMAGE` its `BIOCHEF_CONTAINER_IMAGE`. Under rootful podman this file decides the user, the image, the limits and SELinux. The agent reads it to check that its own settings agree and refuses to start if they do not, so leave `BIOCHEF_PODMAN_SELINUX`, `BIOCHEF_STEP_PIDS` and `BIOCHEF_STEP_MEMORY` unset or equal to it.
4. Allow the agent's user exactly that script, and nothing else, in sudoers, then set `BIOCHEF_PODMAN_HELPER` to the same path:
   `biochef ALL=(root) NOPASSWD: /usr/local/libexec/biochef-agent/podman-step-root`
5. Install `catatonit` if the distribution has it only as a recommendation of podman, as Debian 11 does: the steps run with `--init`.

The script runs a workspace only if it is a directory directly under `RUN_ROOT`, named by no symlink, owned by the agent's user and in the steps' group. Before a run, the agent puts the workspace in that group and gives the group the owner's permissions. Snakemake and the steps then run with umask `007`, so what they write stays readable by the agent and by nobody else. After a run, the script removes the run's containers and gives everything in the workspace back to the agent's user, whatever modes a step left, so the agent can delete it. CI sets this up in a Debian 11 container with its podman 3.0.1 and checks it for real: the sudo rule, the script's refusals, and from inside a step the user, the read-only root, the limits, a memory overrun, the network and another run's directory (`ci/podman-rootful.sh`).

`BIOCHEF_PODMAN_SELINUX`, or `SELINUX` under rootful, must be set, because the right value depends on the host:

- `disable` runs each step with `--security-opt=label=disable`: no SELinux separation for the container, and the workspace mount works without relabelling anything. The other confinements stay: no network, no capabilities, `no-new-privileges`, the read-only root, the limits, the step's user and only the run's workspace mounted.
- `keep` leaves podman's SELinux labelling on, so a step runs confined as `container_t`. The workspace is not relabelled, so on a host with SELinux enforcing, the directory runs are made in (`BIOCHEF_RUN_ROOT`) has to carry a label containers may use, such as `container_file_t`, or every step fails with permission denied. On a host without SELinux it changes nothing.

### The TES API

`python -m tesapi` is a listener of its own that lets the TRE's workflow engines send the node a GA4GH TES task: one command in one image, with files in and out. It is not part of the agent's API. The agent's API serves federations and editors, this serves the TRE's own tools, and the two share no credential, queue, slot or port. It keeps its tasks, and its place as the one listener of the database, in two tables of the node's database and opens no other. It is for a TRE that already has a workflow engine speaking TES, such as Nextflow's TES executor, and wants the node to run what it sends under the node's own guarantees.

It refuses first:

- **Every route needs a token, service-info included.** `Authorization: Bearer <token>`, compared against the SHA-256 in `BIOCHEF_TESAPI_TOKENS`, so a copy of that file is not a way in. `python -m tesapi token <name>` makes a token, shows it once, and prints the line to add. There is no setting that turns authentication off, and `/docs` and `/openapi.json` do not exist. The listener starts without `BIOCHEF_AUDIT_LOG` or any list below set: none has a default. Listening on anything but this host needs `BIOCHEF_TESAPI_TLS_CERT` and `BIOCHEF_TESAPI_TLS_KEY`.
- **Only listed images.** A task runs only an image in `BIOCHEF_TESAPI_IMAGES`, each `repository@sha256:<digest>`. A tag, another digest, or a second executor is refused. The runner has to be able to run them: rootful podman runs only the image in its configuration file, and `subprocess` runs none, so the listener does not start with them.
- **Only listed directories, and only through `file://`.** Inputs are read from `BIOCHEF_TESAPI_READ_DIRS` and outputs written to `BIOCHEF_TESAPI_WRITE_DIRS`, two lists that may share no directory: the same place, or one inside the other, and the listener does not start. So nothing a task wrote can be read back as an input, and nothing it read can be overwritten. Any other URL scheme is refused. A path is accepted only if it is already what it says: normal, absolute, behind no symlink. Do not list a directory that anyone the operator does not trust can write to, because a path can be swapped between the check and the copy.
- **Never the node's own data.** The listener does not start if a listed directory contains, or lies inside, `BIOCHEF_LOCAL_ROOT` (where studies' datasets are bound), `BIOCHEF_RUN_ROOT`, the audit log, the database file, the node's keys, `BIOCHEF_TES_STAGING`, the token and TLS files, or the apptainer cache. Each task is checked against the same list again. A task has no way to name a dataset binding, because the listener never reads them.
- **Nothing of the caller's goes into the step.** `env`, `stdin`, `stdout`, `stderr`, `volumes` and `resources` are refused, not ignored, as is anything the listener does not understand. The command is a list of arguments, written into the step as one quoted command.

Then it runs the task like any other step. The task becomes a Snakefile with one rule and goes through `BIOCHEF_RUNNER`, the same runner and image mechanism as a federated step, so its network, filesystem and environment guarantees are that runner's, and `BIOCHEF_REQUIRE_NETWORK_ISOLATION` is applied to the listener too. At start it records `node.runner.isolation_not_required` if that rule is off, and runs the same network check as the agent. The task runs in a workspace of its own, which is removed. Paths in a task are `/work/<file>`: the command runs in the workspace, so `/work/x` and `x` are the same file, and an argument that starts with `/work` is written as the workspace's path. A path inside a shell script in an argument is not.

Its queue and slots are its own: `BIOCHEF_TESAPI_MAX_CONCURRENT` run at once, `BIOCHEF_TESAPI_MAX_QUEUED` wait, and the next gets 503 with `Retry-After`. They are separate from the agent's `BIOCHEF_MAX_CONCURRENT_RUNS` and from the requests the coordinator sends, though not from the machine's CPU and memory.

What TES asks for and this does not do: `exit_code` is Snakemake's, not the tool's, so any failure is 1 and the tool's own message is in `stderr`; logs arrive when the task ends; a caller sees only its own tasks; the newest 1000 are kept in the database (`tes_task`, migration 0006, and `tes_listener`, migration 0007, so run `python -m store migrate` first or the listener refuses to start), with the URLs and not the inline content of their inputs, and one that was queued or running when the listener stopped is read back as `SYSTEM_ERROR` with the reason in `system_logs` and an audit event, because nothing of it is left to finish; one listener holds a database at a time: a second one started against it refuses to start and says which pid on which host holds it, because starting would end the first one's running tasks. The first renews a lease in `tes_listener` every 10 seconds and gives it up when it stops cleanly; one that was killed is replaced after 30 seconds without a renewal, and the replacement records `tes.lock.acquired` with whom it replaced. A listener that finds its place taken, or cannot reach the database for most of the lease, stops taking tasks, cancels the ones it has and records `tes.lock.lost`. Listeners on different machines that share a database need clocks that agree to a few seconds; a `DIRECTORY` output is merged into the destination, replacing plain files and refusing anything else.

The audit trail gets, with `"source": "tes"`: `tes.start`, `tes.task.created` (the image, the URLs and a SHA-256 of the command, never the arguments, which can hold secrets), `tes.task.state` at every state, `tes.task.refused` and `tes.auth.refused`. The last is written at most once a second, with the count of those it did not write. If the trail cannot be written the task is refused with 503.

### What each runner guarantees

Each runner says, for every step, whether the agent keeps it off the network, whether it sees only its workspace and image, and whether it inherits the agent's environment. Each is `enforced` (the agent applies it to every step, and a step it cannot apply it to fails), `attested` (the operator says the backend applies it, and the agent cannot check) or `none`. The Node screen shows the three.

| Runner | Network | Filesystem | Environment |
|---|---|---|---|
| `subprocess` | none | none, the host's | none, inherited |
| `apptainer`, `singularity` | enforced with `--net --network none` in `BIOCHEF_APPTAINER_ARGS`, the default | enforced with `--contain` or `--containall` | enforced with `--cleanenv` or `--containall` |
| `podman` | enforced | enforced | enforced |
| `tes` | attested, if the operator says so | attested, if the operator says so | enforced |

`BIOCHEF_REQUIRE_NETWORK_ISOLATION` is on unless set to `false`. On, the agent and the page refuse to start with a runner whose network guarantee is not `enforced`: `subprocess`, `apptainer` with the isolation flags removed, and any runner that only attests it. An attested isolation does not count, because the rule is for the case where nobody checks. So the default runner, `subprocess`, needs `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false`, for development or a host with no container runtime. That choice is explicit, and the API process records it at every start as `node.runner.isolation_not_required` in the audit trail, with the runner it let in.

At start, the API process runs one step through a runner whose network guarantee is `enforced`, the way every step runs. The step counts the IPv4 routes and IPv6 addresses on anything but loopback, read from its own network namespace, which must be none. With `BIOCHEF_NETWORK_PROBE=host:port`, the agent first connects to it itself, and the step then tries to as well, which must fail. Without the probe, only the interfaces are checked. The result goes to the audit trail as `node.runner.network_check` and, on a node, to the Node screen. If the step finds the network, the agent does not start. This catches a host where the isolation flag is accepted and has no effect.

`BIOCHEF_RUNNER=singularity` is the same runner with SingularityCE, for a host that has it and not apptainer. The flags and `BIOCHEF_APPTAINER_ARGS` are the same. Both runners need `BIOCHEF_APPTAINER_BINARY`, the absolute path of the binary, with no default. Snakemake has no setting for it: it runs `apptainer` if one is on `PATH` and `singularity` otherwise, and apptainer's packages install a `singularity` that is apptainer. So the agent checks what the binary says it is (`apptainer version` or `singularity-ce version`) and refuses to start on a mismatch. It then puts a link to it, named `apptainer`, first on Snakemake's `PATH`, in a directory of its own.

With `--net --network none`, SingularityCE makes the network namespace and sets nothing up in it. A user who is not root may always ask for that network, and no other one unless `singularity.conf` allows it. The `.deb` installs a setuid starter. With `allow setuid = no` in `singularity.conf`, SingularityCE uses unprivileged user namespaces instead, which on Ubuntu 24.04 its AppArmor profile allows. CI runs the parity workflow and the network check through it both ways, as a user that is not root.

`BIOCHEF_APPTAINER_ARGS` defaults to `--contain --cleanenv --net --network none`. The last two give each step a network namespace with only loopback in it, so a tool cannot send data anywhere. On a host where apptainer refuses them the run fails, because carrying on with the network would be the worse outcome. Without `--contain`, apptainer binds the host's `/tmp` into the container, and that is where a run's directory lives unless `BIOCHEF_RUN_ROOT` says otherwise. Without it, a containerised tool is walled off from `/usr` and `/etc` while still able to read **every other run's data**. Without `--cleanenv`, the tool process also inherits the Agent's environment. Emptying this variable turns both protections off deliberately.

### The TES runner

`BIOCHEF_RUNNER=tes` sends each step to a GA4GH Task Execution Service that already exists, such as Funnel, as one task. Snakemake stays on the host and uses the `tes-step` script as the shell its rules run under, as `podman-step` is for podman. The agent keeps what it can check for itself:

- **The image is only ever given by digest.** `BIOCHEF_TES_IMAGE` must be `repository@sha256:<64 hex digits>`, with no scheme. A tag can point to another image tomorrow, and the agent refuses one. The image needs `bash`, `find`, `sha256sum`, `sort`, `comm` and `cp`, which the Debian image used in CI has.
- **The task holds nothing of the agent's.** It is built from the step's command and the image alone: no `env`, no credential, no stdin. The server's credential is in a file only the agent's user can read, `BIOCHEF_TES_TOKEN_FILE` for a bearer token, or `BIOCHEF_TES_USER` and `BIOCHEF_TES_PASSWORD_FILE` for basic authentication, and travels only in the `Authorization` header. A redirect is not followed, and the agent's proxy settings are not used, so the credential goes to the configured server and nowhere else. Neither the token nor the password is ever in the task, and the step is given the path of the file, not what is in it. The URL must be https, or http to this host.
- **The server is shown a copy.** Each step gets a directory of its own, `BIOCHEF_TES_STAGING/<run>/<step>/in`, with a copy of the run's workspace except `Snakefile` and `.snakemake`. A link or a special file in the workspace stops the step, because a server running as root would follow it. The task sees the copy at `/in`, read-only because Funnel mounts every input so, copies it to a scratch directory of its own, runs the step there, and writes what the step changed to `/out`, the one directory it may write. On the server, allow `file://` for that directory only.
- **What comes back is checked before it is placed.** The task hashes the workspace before and after the step and returns only the files that are new or changed, with a manifest of their SHA-256. The agent checks that every file is a plain file, that the manifest and the files agree exactly, and that no path leaves the workspace or names the agent's `Snakefile` or `.snakemake`. If one check fails, nothing from that step is placed. The manifest is written by the same task as the files, so it proves that nothing changed on the way back, not that the step was honest. A step that deletes a file does not delete it in the workspace. The step's output arrives when the task ends, not as it is written.
- **The files come back owned by whoever the server runs as.** A server running as root returns files the agent can read and cannot delete. `tes-step-root` is called through one sudo rule and gives the step's directory back to the agent's user, as `podman-step-root` does for rootful podman:
  1. Install it owned by root, in a directory owned by root, neither writable by anyone else:
     `install -o root -g root -m 0755 tes-step-root /usr/local/libexec/biochef-agent/tes-step-root`
  2. Write `/etc/biochef-agent/tes-step.conf`, owned by root and writable only by root, in a directory that is too. The path is fixed in the script. It has one line, `STAGING_ROOT=`, and that must be the agent's `BIOCHEF_TES_STAGING`, a directory the agent owns that others cannot enter, named by no symlink.
  3. Allow the agent's user exactly that script in sudoers, and set `BIOCHEF_TES_HELPER` to the same path:
     `biochef ALL=(root) NOPASSWD: /usr/local/libexec/biochef-agent/tes-step-root`

  The script has one command, `finish <directory>`. It runs only on a directory two levels under `STAGING_ROOT`, named by no symlink and owned by the caller, and it only changes who owns the files. The agent refuses to start as root with this runner, and unless the file, the script and the sudo rule are as above.
- **A killed run does not leave its task running.** The group kill reaches `tes-step` and not the server. When a run ends, the agent lists the server's tasks named `biochef-<run>-`, cancels those that have not finished, waits for them, and clears the run's staging. A `SIGTERM` to `tes-step` cancels its task too.

The agent asks the server `GET /v1/service-info` at start and refuses to start on anything that does not answer as a TES service.

`BIOCHEF_TES_ISOLATION` has no default, because it is the operator's statement and nobody else can make it. `attested` says the server gives every task no network and only its input directory. The agent cannot check that, so the network and filesystem guarantees are `attested`, and the environment guarantee is `enforced`, because the task is built here without it. `BIOCHEF_REQUIRE_NETWORK_ISOLATION` does not accept an attested isolation, so the runner starts only when that rule is turned off with an explicit `false`, which is audited at every start with the runner. `none` says the server gives no such thing, and both guarantees are then `none`.

CI runs the runner against two TES servers, each running as root and starting its tasks in a docker container with no network, with the same check (`ci/tes_check.py`). It checks the image the steps ran in, that another run's directory and the agent's environment are not there, that the files came back owned by root and were the agent's after the helper, that a run that times out has its task cancelled through the server and listed there as `CANCELED` by `name_prefix`, and that no container is left.

- **A stand-in** (`tests/tes_support.py`) speaks the part of TES the agent uses and is what the unit tests run against too.
- **Funnel v0.12.2**, built in the job from the commit its tag names (`d5f0655`; the tag has no release, binary or public image), configured from its own default config as an operator who attests network isolation would (`ci/funnel_config.py`). The job runs twice, with Funnel's authentication off and with its basic authentication on (`BIOCHEF_TES_AUTH=basic`, a user and a password in a file). With it on, the check also shows that Funnel refuses a request with no credentials and one with the wrong password. Funnel's OIDC mode is not exercised, and the bearer path is proven only against the stand-in.

One thing only the real Funnel could show, and the runner follows it: it mounts every input read-only, which is why the step runs in a copy. The agent checks paths itself and does not rely on the TES server's directory checks.

## The audit trail

`BIOCHEF_AUDIT_LOG` names a file that records every state a run reaches — who
asked, which run, which state, when, and the error if one ended it. One JSON
object per line, appended and never rewritten. Unset writes nothing, which is
the existing behaviour and stays the default.

```json
{"schema":"biochef.audit-event.v1","event":"run.state","at":"2026-08-28T10:11:12+00:00","authenticated_by":"bearer","caller":null,"error":null,"run_id":"9f3c…","state":"COMPLETE"}
```

The record is written at the single point where a run's state changes, not at
each call site, because a hook per call site is one a new transition forgets —
and a gap in an audit trail is invisible from inside the thing that should have
written it.

Three things it deliberately does not claim:

- **It is append-only, not immutable.** Anything that can write the file can
  truncate it. Immutability is the filesystem's job: ship the file somewhere
  append-only, or set the append-only attribute. Claiming otherwise would be the
  dangerous kind of assurance.
- **`caller` is whatever the authentication provider knew**, which for `bearer`
  is nothing — a shared secret is not an identity and every holder is the same
  caller. The event records which provider authorised the request, so "we do not
  know who" reads as a fact about the deployment rather than a hole in the log.
  A provider that carries identity lights this field up without the format
  changing.
- **There is no endpoint that serves it.** An audit trail reachable over the
  same API it audits is a thing an attacker reads to find out what you noticed.
  Export it from the filesystem.

If the trail cannot be written, the run fails rather than proceeding unrecorded.
A deployment that believes it is auditing and is not is in a worse position than
one that knows it is not.

## Passports

`BIOCHEF_AUTH=passport` accepts a GA4GH Passport instead of a shared secret, and
names the caller `<issuer>#<subject>` — with the issuer, because two brokers can
each have a subject `12345` and recording only the second half would merge two
people into one caller.

It is the first provider that genuinely names anyone, so it is the first to
declare that what it returns is a name and not a marker every caller shares.
That declaration is what puts the caller in the audit trail; `none` and `bearer`
decline it, and a provider that says nothing at all refuses to start.

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

## The GA4GH WES surface

The same runs, at the standard path, so third parties do not have to learn this
service's own API:

```
GET  /ga4gh/wes/v1/service-info
GET  /ga4gh/wes/v1/runs
POST /ga4gh/wes/v1/runs
GET  /ga4gh/wes/v1/runs/{run_id}
GET  /ga4gh/wes/v1/runs/{run_id}/status
POST /ga4gh/wes/v1/runs/{run_id}/cancel
```

It is a mapping, not a second implementation: every endpoint reads the same run
store the bespoke API reads, and a run submitted through either is the same run.
That is why the run states are WES's `RunState` vocabulary and why cancel was
already at WES's path. The bespoke endpoints stay — the editor uses them.

**The declared workflow type is `BIOCHEF`, not `CWL`.** This service runs
BioChef workflow documents converted to Snakemake; it cannot run CWL, and saying
otherwise would make every conformant client discover this server, upload its
data, and then fail. `service-info` exists so a client can find that out before
it commits, so a submission naming another type is refused by name with a
message saying what would have been accepted.

`service-info` also lists, under `tags.not_implemented`, where the standard
surface stops: `workflow_url` may only name an attachment (a remote URL is not
fetched, which would mean executing a document from wherever a caller pointed
this service), run listing is not paginated, and `task_logs` carries one entry
per workflow step without per-task commands.

Tool parameters stay in the frontend's workflow JSON. `workflow_params` accepts
only an empty object (`{}`); malformed or non-empty values return HTTP 400.
Submission tags are not retained. WES submissions use the same admission limit
and HTTP 503 capacity refusal as `/runs`.

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

## The node's store

What a node has decided lives in a database, because an approval has to outlast the process that made it. `store/` holds the tables of [the agent UI specification](docs/agent-ui/SPEC.md), section 6.1, and all access goes through `store/repo.py`. The API does not use it yet.

```sh
export BIOCHEF_DB_URL=sqlite:////var/lib/biochef/agent.db
python -m store migrate                  # create or upgrade the tables
python -m store downgrade base           # remove them
python -m store.seed --fixture mock      # invented data, for development only
```

SQLite only, in WAL mode, because two processes open the file. Any other `BIOCHEF_DB_URL` is refused at start: concurrent approvals rely on SQLite's write lock, and no other database has been tested. The seed refuses a database that already holds data, and signs its decisions with a key it generates and throws away.

## The node's keys

A node has two keys: Ed25519 signs its decisions and results, and X25519 decrypts the payloads of requests. Create them outside this repository:

```sh
python -m node.keys init --out /etc/biochef/keys
```

It prints both public fingerprints, for registering the node, and refuses to replace a key that exists. The files must stay readable by their owner only. Point `BIOCHEF_NODE_SIGNING_KEY` and `BIOCHEF_NODE_ENCRYPTION_KEY` at them; a key that is missing, exposed, inside the repository or of the wrong type is refused.

## The page for a node's data manager

A second process, on its own port, for the people who run a node inside a TRE. It shares the database and the audit trail with the API and nothing else: it never mounts the API's routes, and the API never serves it. So far it has the Inbox, Studies and the workflow screens, where study and workflow versions are approved, refused, withdrawn and undone. Requests, Datasets and Node are still empty.

```sh
./run-ui.sh          # uses the settings in the environment or .env
./run-ui.sh --dev    # a seeded database and a fixed user on 127.0.0.1, marked in red on every page
```

`--dev` keeps its database, keys and audit trail in `~/.cache/biochef-agent-dev`. Its seed is invented data, and its Policy Engine is a fake that the page labels as one. Delete that directory for a fresh seed.

It needs Python 3.11, 3.12 or 3.13 and refuses to start on any other.

The agent has no accounts. The TRE decides who may open the page, and `BIOCHEF_UI_IDENTITY` says how the agent hears about it:

- `header`: a reverse proxy passes the user name in `BIOCHEF_UI_IDENTITY_HEADER`. The header counts only from an address in `BIOCHEF_UI_TRUSTED_PROXIES`. Anything else is a 401, static files included.
- `dev`: `BIOCHEF_UI_DEV_USER`, from a loopback address only. It refuses to start on any other `BIOCHEF_UI_HOST`.

The page refuses to start without `BIOCHEF_AUDIT_LOG`, because every decision made there has to reach the trail, and without the node's signing key or a migrated database. `run-ui.sh` starts `python -m ui`, which keeps uvicorn's proxy headers off. If you run `uvicorn ui.app:app` yourself, pass `--no-proxy-headers`, because otherwise uvicorn rewrites the client address that the identity check depends on.

Everything the page loads is served by the agent: htmx, the fonts and the stylesheet are in `ui/static/`, and `ui/static.sha256` records the vendored files. `ui/static/app.css` is built and committed, so nothing else is needed at runtime. After changing a template or `ui/tailwind/input.css`, run `make css`, which downloads the pinned Tailwind CLI, checks it against `ui/tailwind/SHA256SUMS` and rebuilds the file. CI fails if the committed file differs.

## The site list

`node/federation.py` keeps the site list, the list of nodes in the federation, only with a valid signature from `BIOCHEF_FEDERATION_KEY`, and refuses a version older than the newest one held, because going backwards is how a removed node comes back. Nothing receives a list yet: the coordinator client does that. The format is an assumption until the coordinator defines it, and the module says which one.

## The Node screen

The Node screen shows the keys' fingerprints, the site list, the coordinator, the runner and the settings, all read only, and the newest 50 events of the audit trail. "Export" downloads the whole trail. The page process reads the file itself, and the API still has no route to it.

A decision's audit event is written before its transaction commits, so a commit that fails leaves an event for a decision that does not exist. The screen crosses the trail with the `decision` table and lists any such event. Nothing repairs the trail. An event is matched on its decision id, subject, action and user, and one followed by an undo is accounted for.

## Egress

A node sets `BIOCHEF_EGRESS_MODE` to `airlock` or `manual`. That also turns on the mirror: the API process copies each run into `run_summary`, carries out cancellations asked for on the page, and rebuilds retained workspaces after a restart, because the database is the only channel between the two processes. A plain agent leaves it unset and needs no database.

Output rules judge every file first, and a file held back never leaves in either mode. Then the TRE's airlock decides, or a data manager releases or withholds on the Run screen with a signed decision. A released run is encrypted for the member who submitted the request (`result_recipients: submitter`, the only value for now), signed with the node key and posted. A withheld one gets a signed notice with no files, made when it is withheld and kept until the coordinator can be told.

The retention clock of `BIOCHEF_KEEP_OUTPUTS` starts at `sent` or `withheld`, not when the run ends. A run waiting for egress is never evicted, and when such runs fill `BIOCHEF_MAX_RETAINED_RUNS` a new run gets 503.

The Policy Engine, the airlock and the coordinator's result endpoint are protocols in `node/` with fakes. Until the Policy Engine is connected, the agent checks `types` and `max_bytes` itself, from each file's name and size, and marks `min_count` and `aggregates_only` as not verified on every file that passes. A data manager releasing such a run ticks an explicit confirmation, and the signed decision names the rules that were not verified; in airlock mode the airlock receives the same marking. Without an airlock, an airlock node's runs wait: nothing skips that stage.

## Requests

A request is checked in the ten steps of the specification (section 6.3), and the first failure stops the rest. Every verdict writes an audit event, and a refusal is also a signed decision. A repeat is the same `request_id`, which the member signs: delivered again, it joins the run of an accepted request and gets the same refusal as a refused one, and only a count is kept. A new `request_id` is a new request even with the same payload, and the same `request_id` on a different signed request is refused as `replay`. Check 5 comes first, so a request_id is held to this until its `not_after`. `BIOCHEF_PASSPORT_REQUIRE_VISA` names the visa type required for each dataset, and the visa's value must be exactly the `visa_value` of the dataset's binding, its subject the member, and its issuer one of `BIOCHEF_PASSPORT_VISA_ISSUERS`. A binding with no `visa_value` matches no visa.

The payload is decrypted with `pyrage`, and an accepted payload is kept in memory only.

The coordinator's agent API does not exist yet, so `node/verify.py` lists three details of the format that are assumptions until it does.

## Dataset bindings

A study names datasets by id, and a binding says which folder under `BIOCHEF_LOCAL_ROOT` holds one. Both ways of binding need `localpath` in `BIOCHEF_DATA_SOURCES` and an existing `BIOCHEF_LOCAL_ROOT`.

```yaml
# BIOCHEF_DATASET_BINDINGS_FILE
EGAD00001009912:
  type: localpath
  location: cardio/2019_2024/
  visa_value: https://dac.example.org/datasets/EGAD00001009912
```

`visa_value` is the exact value the data access committee issues in a visa for this dataset, usually a URL. On a node with `BIOCHEF_PASSPORT_REQUIRE_VISA`, a request is compared with it and with nothing else, so a dataset with no `visa_value` refuses every request as `visa_missing`, and the Datasets page says so.

The file is read when the page starts. Its bindings are locked on the page, and it wins over anything bound there: a page binding it replaces gets a signed `unbind`, and the page reports it. With `BIOCHEF_DATASET_BINDINGS_FROM_UI=true` a data manager can bind, edit and remove the others, each as a signed decision. Anything that resolves outside the root is refused, with the same check `localpath` applies to a run's inputs.

A binding names a folder and a run reads files: `node/datasets.file_in_binding` turns a file named relative to the dataset into a `localpath` path, and refuses anything that leaves the dataset's own folder.

## Member identity

A study version lists each member's keys next to an LS Login ID token. `node/identity.py` checks the token: signed by LS Login, through the keys at `BIOCHEF_LS_LOGIN_ISSUER`, made for `BIOCHEF_EDITOR_CLIENT_ID`, unexpired, and with a `sub` equal to the member's `ls_aai_id`. A member's name comes only from the `name` claim of a token that passed; the editor asks for the scope `openid profile` so that the token carries it.

The token's nonce ties it to the member's keys. The agent builds the object `{"v": "biochef.member-keys.v1", "result_key": ..., "signing_key": ...}` from the keys the study version lists, serializes it with RFC 8785, and the nonce must be exactly the SHA-256 of those bytes in base64url without padding. `signing_key` is the public JWK with only `crv`, `kty`, `x` and `y`, the coordinates as 43 characters of base64url with their leading zeros, and `result_key` is the age recipient in lowercase. Keys in any other form are refused, not normalised. A member whose nonce does not match counts as not verified, with that reason shown on the page, and leaves no name. The test vector is in `tests/test_node_identity.py`.

## Upgrading

`UPGRADING.md` says what an existing deployment has to change, and why, and `CHANGELOG.md` lists the changes. Read both before updating an agent that is already installed: since `BIOCHEF_REQUIRE_NETWORK_ISOLATION` became on by default, an agent that runs `subprocess` stops at start until its configuration says what it wants.

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

### Converter intermediate document

`BIOCHEF_WRITE_INTERMEDIATE` defaults to `false`. When enabled, the Agent
writes and validates `intermediate.json` inside each run workspace before
generating its Snakefile. Workspace retention follows `BIOCHEF_KEEP_WORKSPACE`.
The document carries tool provenance and effective parameter values, which may
be sensitive; it has the same local access boundary as the run workspace.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
