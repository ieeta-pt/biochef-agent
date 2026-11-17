# Threat model

What this service assumes about who is trusted, so that a change can be argued
about on the same terms twice running.

This document describes the model, not a claim that every boundary is enforced.
Which parts are enforced changes with every merge; what the agent is *for* does
not. Check the source and deployment configuration before treating a control
as active. Gaps are named with the issue that tracks them.

What `master` runs today is an HTTP service: a caller sends a workflow to
`/convert`, `/runs` or the WES endpoints and reads the results back over the same
API. `BIOCHEF_AUTH` decides who may call it: `none` by default, a shared
`bearer` token, or a GA4GH `passport`. It has no coordinator client yet.

In a TRE the agent is deployed the other way round. Nothing connects into a
TRE: the agent runs inside it, opens every connection, and polls the
coordinator over mTLS with a per-site certificate from the federation PKI,
checking the coordinator's certificate against the federation CA. Researchers
never talk to the agent. The same trust questions apply to both flows, but in
the TRE the destination and the authorization decision move to the
agent-to-coordinator boundary. What that deployment needs and `master` does not
have yet is listed under [Planned](#planned).

## What the agent is for, and why that decides everything else

The agent takes a workflow, or individual steps of one, to where the data and
the compute already are: a Trusted Research Environment, an HPC cluster, an
institutional server. Computation goes to the data because the data cannot come
to the computation — it is controlled-access, or too large to move, or both.

Every judgement below follows from that. An executor deployed next to data that
may not leave is not the same thing as a public API with a sandbox around it,
and reasoning about it as though it were produces the wrong answers. It has
already done so at least once: several findings were dismissed on the grounds
that they required "code already running on the host", which describes the
ordinary case here rather than an escalation.

## Deployment responsibilities

The required protections also form a security contract between the Agent and the site. Data owners and the site's governance process decide permitted use and release and the Agent applies those rules, supported by site-managed controls.

The TRE defines which tools may run and who may access each dataset. The data access committee (DAC) decides data access. The node's data manager then approves each study, version by version, and each workflow in it, and the agent signs each decision with the node key. For each job, the Agent checks that a member of an approved study version signed it, that it runs an approved workflow, and that the member's visas allow the datasets it reads. It runs the job only if local rules permit it, and checks permission to release results to the study's result recipients.

The TRE must provide trusted policy and configuration, approved data-access permissions, and an execution environment that isolates jobs and enforces CPU, memory, disk and process limits. It also defines retention rules, protects audit records and provides the local output-release procedure. Execution limits must hold at the host, storage or scheduler, not only in the workflow description.

The Agent must use those controls when admitting and running a job. A workflow or coordinator cannot override local rules. If required permission, artifact verification or execution protections cannot be established, the Agent must refuse the affected execution or release.

## Actors

**The member** is a researcher listed in a study, with a signing key and a
result key tied to their LS Login identity. A job is a member's signed
submission. The Agent must check that the signing key is the member's and that
the member may use the requested data. It is important to consider that an
authenticated member can still submit a hostile job.

**The data manager** is the person at a node who approves studies and
workflows, in the agent's page inside the TRE, with the TRE's own login. The
DAC decides data access, and the data manager approves once it has. Each
decision is signed with the node key. Audit records must identify who decided.

**The caller or coordinator** supplies a workflow and its inputs. Untrusted as a
source of tasks even after its identity is established. The coordinator stores
and delivers studies, jobs and results, and never reads a job or a result. It
may relay only jobs a member signed, and the Agent must verify the signature
and the approved study version rather than accept the coordinator's word.
Approval for one job does not permit additional or changed jobs. On `master`
today the caller is whoever reaches the HTTP API.

In a TRE, the Agent and the coordinator authenticate each other with mTLS, and
the Agent authorizes each task against local data and execution policy.
Authenticating the coordinator does not establish a member's identity or
permissions. Audit records must identify the member who submitted each job and
the data manager who approved it.

**The identity provider** is LS Login, always: every researcher logs in with
it. BioChef's Keycloak sits in front of it, brokers it as its only identity
provider and issues the tokens the coordinator trusts; it is never an identity
provider itself. Nodes never use the Keycloak token as authorization. They rely
on the members' LS Login ID tokens, the visas and mTLS. Identity checks do not
by themselves prove job approval or data access. On `master`,
`BIOCHEF_AUTH=passport` checks a GA4GH Passport and verifies each of its visas
against issuers the deployment names (**#79**); a required visa is a condition
of being let in, not an authorization model.

**The workflow description** is task-supplied data. Its node ids, parameter
values, and edge handles may reach a generated Snakefile or a filename, so they
must be validated rather than trusted as a description.

**The tool bundle** comes from the registry, and the task selects which one:
the `repo` for each node is a field in the request. The Agent resolves every
tool through the hub's signed catalogue, which decides which manifest a package
is; the catalogue is required by default since **#100**
(`BIOCHEF_TOOL_CATALOG=required`). It checks pulled bytes against manifest
digests (**#9**) and offers signature and evidence verification in `strict`
mode (**#14**). Signature checks are still off by default
(`BIOCHEF_SIGNING_MODE=off`); a TRE deployment must enable them with an
appropriate local policy. Verification does not prove that a correctly signed
tool is safe. Bundle-supplied strings remain untrusted input (**#42**).

The TRE decides which tools may run under its local policy. Before execution, the Agent must check that policy permits the selected tool artifact for the job.

**The tool binary, once running, is untrusted.** This is the one most easily got
wrong. It is arbitrary compiled code, fetched from a registry. With the default
subprocess runner it executes with the Agent's privileges against whatever the
deployment can see. The optional container runner changes that access but does
not itself establish complete confinement. A tool can read, write, and spawn
processes wherever its runtime permits. "The attacker is already executing
code on the host" is not a precondition to be argued away here; it is Tuesday.

In a TRE, each workflow step must run in a container of its own, with no
network, only its own workspace and a clean environment, and the Agent must
refuse a runner that cannot enforce that, unless the TRE turns the rule off on
purpose. `master` does not enforce that rule yet; see [Planned](#planned).

Tools must receive only the data and resources approved for their run. They must not inherit the Agent's credentials or access its configuration and policy.

**The data** is the asset. In the deployments this is written for it is the
reason the environment exists, and the reason it may not leave.
Results, intermediate files and logs can also be sensitive and must remain inside the TRE until local policy approves release and Agent audit records must not contain raw research data, access tokens or secrets.

**The operator** — whoever deploys and configures the agent — is trusted. So is
the machine it runs on and the environment variables it is given.

In a TRE, only its administrators may configure the Agent and its policy. The Agent trusts this administrator-controlled policy; a researcher, workflow or coordinator cannot replace it with rules supplied in a job.

## Keys and envelopes

In a TRE, trust in a job rests on keys, not on the connection it arrived over.

- The node has two keys that never leave it: an Ed25519 approval key that signs
  its decisions and results, and an X25519 encryption key that jobs are
  encrypted for. `master` has both, and writes every decision it signs to the
  audit trail (**#109**).
- A member's signing and result keys are tied to them by an LS Login ID token
  whose nonce is the hash of the keys. The node checks that token before the
  data manager approves a study version.
- A job is signed by a member and encrypted per node, and each node's result is
  signed by the node and encrypted for the study's result recipients. The
  coordinator stores and delivers both and can read neither.
- The node checks the member's visas for every dataset a job reads, against
  that dataset's visa value, exactly. Source of visas: inside the job, or
  fetched by the node with LS Login offline access (pending the refresh token
  test). The check itself does not change.

Apart from the node keys and signed decisions, these checks are
[planned](#planned).

## Boundaries

| boundary | what crosses it | what has to hold |
|---|---|---|
| coordinator → agent, polled by the agent over mTLS | study versions with the members' keys and ID tokens, and jobs signed by a member and encrypted for this node; on `master` today, workflow JSON and uploaded files or data references sent to the HTTP API | authenticate both ends; verify the member's signature, the approved study version and workflow, and the visas, then authorize the task locally; validate names, inputs and shell-bound values |
| identity evidence → agent | members' LS Login ID tokens and GA4GH visas | check each token's issuer keys, audience, subject, nonce and expiry; check each visa's issuer, value, age and that it belongs to the member; apply local access rules |
| registry → agent | the signed catalogue, bundle metadata, tool binaries | verify the catalogue, immutable artifact identity and required evidence, and check local tool approval before execution; still treat content as untrusted |
| agent → tool | a working directory, argv, local data access | confine filesystem, process privileges, and network access to the approved run |
| tool → agent | files in that directory | read as data, never followed out of the run |
| agent → caller/coordinator | on `master`, the response body; in a TRE, results signed by the node and encrypted for the result recipients | apply local output policy before any data leaves; authorize the destination and record the transfer |

That last row is the one worth dwelling on. On `master`, the response body is
a data-exit path. In a TRE, the result sent to the coordinator is the
corresponding path. Anything that lets a tool cause
the Agent to read a file it did not produce — a symbolic link, a hard link, an
output slot pre-filled by an upload — can turn an authorized result channel into
exfiltration. Findings of that shape are not "the caller deceiving itself";
they are the thing the environment exists to prevent.

## Current limitations

Stated plainly, because a threat model that implies more coverage than it has is
worse than none.

These are gaps or limits of assurance, not excluded threats. A protected TRE deployment must supply the required isolation and resource controls.

- **A malicious tool escaping its working directory.** Snakemake's `--directory`
  sets an origin, not a jail: a rule whose output is `../escaped` can write
  outside it. A container runner now exists (**#15**), but the default runner is
  still a subprocess and this document does not establish that a deployment
  confines all filesystem, process, and network access. A run directory alone
  cannot do that.
- **A malicious but correctly signed tool.** Digest and signature checks can
  bind the bytes to an identity and policy; they cannot establish benign
  behavior or rule out a compromised publisher.
- **Resource exhaustion.** Request-body middleware enforces a configurable
  upload-byte limit (**#11**), and `BIOCHEF_MAX_CONCURRENT_RUNS` caps how many
  runs execute at once. Nothing bounds bytes a run writes to disk, the size of
  a workflow, retained output size, or the tool cache (**#91**). A concurrency
  slot counts runs, not what one run consumes.
- **No authorization between callers.** Authentication names a caller; nothing
  then decides what that caller may read. Any authenticated caller can read any
  run's outputs, logs and manifest by run id (**#90**). Passports (**#79**) make
  callers distinguishable, so on a shared Agent this is access across projects
  today.

## Trusted assumptions and exclusions

- **Anything requiring the operator to be hostile.** Someone who can set the
  environment or write to the tool cache has already won, and defending against
  that is out of scope.

  Oversight of administrators belongs to the TRE; audit records protected from alteration by those administrators can support accountability. This exclusion does not cover malicious researchers or tools, including attempts to gain administrative access.

- **Browser-side verification in the editor.** The editor checks the signed
  catalogue with a public key shipped in its own JavaScript bundle, so its root
  of trust is the frontend deploy pipeline. That is the editor's threat model,
  not this one: the Agent does not rely on anything the browser verified, and
  checks each bundle itself against its own configured policy.

## Planned

Designed in #106 (`docs/agent-ui/SPEC.md`) and in draft PRs. None of it is on
`master`, so none of it is a current protection.

- **Polling the coordinator over mTLS**, so the agent needs no inbound port,
  and posting its signed decisions there (#106, sections 1.1 and 7.5).
- **The data manager's page**, inside the TRE with the TRE's own login, where
  studies, workflows, requests, runs, datasets and releases are decided (#106,
  sections 1 and 8).
- **Request checks**, in order: the member's signature, this node, the approved
  study version and workflow, expiry, replay, decryption, the workflow digest,
  the visas against each dataset's `visa_value`, and the dataset bindings
  (#106, section 6.3).
- **The member identity check**: the LS Login ID token that ties a member's
  keys to them, checked before a study version is approved (#106, section 7.3).
- **Output rules and release**: each output checked against the study's output
  rules by the Policy Engine, then handed to the TRE's airlock or released by
  the data manager. Until the Policy Engine is connected, rules that need a
  file's contents are marked "not verified" (#106, section 6.5).
- **Per-runner guarantees**: each runner states whether a step can reach the
  network, see more than its workspace, or inherit the agent's environment, and
  the agent refuses to start with a runner whose network isolation is not
  enforced unless that rule is turned off explicitly. Covers apptainer without
  network, podman, singularity and a TES runner (#120 to #124; #106, section
  14.1).
- **A TES API** that lets the TRE's own workflow engines send the node a task,
  on a listener of its own (#125; #106, section 14.4). It adds an inbound path
  inside the TRE, which this document will need to cover when it lands.

## Applying it

When judging whether something is a defect, ask:

1. Can a **caller or coordinator** cause it with a task, workflow, or input?
2. Can a **tool** cause it with the access its configured runner grants?
3. Does it end with data crossing the Agent's result or egress boundary, or
   with the Agent's execution being redirected?

A "yes" to 2 counts. That is the correction this document exists to record.
