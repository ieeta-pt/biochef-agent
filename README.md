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
| `BIOCHEF_AUDIT_LOG` | *(unset)* | path to an append-only JSONL audit trail; unset writes none |
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
