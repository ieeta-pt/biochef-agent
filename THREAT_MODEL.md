# Threat model

What this service assumes about who is trusted, so that a change can be argued
about on the same terms twice running.

This document describes the model, not a claim that every boundary is enforced.
Which parts are enforced changes with every merge; what the agent is *for* does
not. Check the source and deployment configuration before treating a control
as active. Gaps are named with the issue that tracks them.

The status of each control is in [Implementation status](#implementation-status)
at the end; the rest of this document describes the design.

The agent runs inside a TRE, next to the data. Nothing connects into a TRE: the
agent opens every connection, and polls the coordinator over mTLS with a
per-site certificate from the federation PKI, checking the coordinator's
certificate against the federation CA. Researchers never talk to the agent.
The agent can also run as a standalone HTTP service that accepts workflows
directly, with `BIOCHEF_AUTH` deciding who may call it. That is a development
and transition mode, outside the TRE deployment this model covers.

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

The TRE defines which tools may run and who may access each dataset. The data access committee (DAC) decides data access. The node's data manager then approves each study, version by version, and each workflow in it, and the Agent signs each decision with the node key. For each job, the Agent checks that a member of an approved study version signed it, that it runs an approved workflow, and that the member's visas allow the datasets it reads. It runs the job only if local rules permit it, and releases results only through the output rules and the TRE's release procedure, to the study's result recipients.

The TRE must provide trusted policy and configuration, approved data-access permissions, and an execution environment that isolates jobs and enforces CPU, memory, disk and process limits. It also defines retention rules, protects audit records and provides the local output-release procedure. Execution limits must hold at the host, storage or scheduler, not only in the workflow description.

The Agent must use those controls when admitting and running a job. A workflow or coordinator cannot override local rules. If required permission, artifact verification or execution protections cannot be established, the Agent must refuse the affected execution or release.

## Actors

**The member** is a researcher listed in a study, with a signing key and a
result key tied to their LS Login identity. A job is a member's signed
submission, encrypted for each node it runs on. The Agent checks that the
signing key is the member's and that the member may use the requested data. It
is important to consider that an authenticated member can still submit a
hostile job.

**The data manager** is the person at a node who approves studies and
workflows, in the agent's page inside the TRE, with the TRE's own login. The
DAC decides data access, and the data manager approves once it has. Each
decision is signed with the node key, and audit records identify who decided.

**The coordinator** stores and delivers studies, jobs and results, and never
reads a job or a result. It is untrusted as a source of tasks even after its
identity is established. It may relay only jobs a member signed, and the Agent
verifies the signature and the approved study version rather than accept the
coordinator's word. Approval for one job does not permit additional or changed
jobs. The Agent and the coordinator authenticate each other with mTLS, and
authenticating the coordinator does not establish a member's identity or
permissions. Audit records identify the member who submitted each job and the
data manager who approved it.

**The TRE's own workflow engines** may send the node a task through the TES
API, a listener of its own inside the TRE that needs a bearer token, and TLS
when it is not on loopback. Its tasks run through the same runner, with the
same guarantees, and it reads no study, request or dataset binding. In this
document, a caller is whoever hands the Agent a task: the coordinator, or one
of these engines.

**The identity provider** is LS Login, always: every researcher logs in with
it. BioChef's Keycloak sits in front of it, brokers it as its only identity
provider and issues the tokens the coordinator trusts; it is never an identity
provider itself. Nodes never use the Keycloak token as authorization. They rely
on the members' LS Login ID tokens, the visas and mTLS. Identity checks do not
by themselves prove job approval or data access.

**The workflow description** is task-supplied data. Its node ids, parameter
values, and edge handles may reach a generated Snakefile or a filename, so they
must be validated rather than trusted as a description.

**The tool bundle** comes from the registry, and the task selects which one:
the `repo` for each node is a field in the request. The Agent resolves every
tool through the hub's signed catalogue, which decides which manifest a package
is, checks pulled bytes against manifest digests, and verifies the signature
and evidence against the site's local policy. Verification does not prove that
a correctly signed tool is safe. Bundle-supplied strings remain untrusted input
(**#42**).

The TRE decides which tools may run under its local policy. Before execution, the Agent must check that policy permits the selected tool artifact for the job.

**The tool binary, once running, is untrusted.** This is the one most easily got
wrong. It is arbitrary compiled code, fetched from a registry. A tool can read,
write, and spawn processes wherever its runtime permits. "The attacker is
already executing code on the host" is not a precondition to be argued away
here; it is Tuesday.

Each workflow step runs in a container of its own, with no network, only its
own workspace and a clean environment. The Agent refuses to start with a runner
that cannot enforce network isolation, unless the TRE turns that rule off on
purpose, and checks at start that a step cannot reach the network. A container
limits what the tool can reach; it does not make the tool trusted.

Tools must receive only the data and resources approved for their run. They must not inherit the Agent's credentials or access its configuration and policy.

**The data** is the asset. In the deployments this is written for it is the
reason the environment exists, and the reason it may not leave.
Results, intermediate files and logs can also be sensitive and must remain inside the TRE until local policy approves release and Agent audit records must not contain raw research data, access tokens or secrets.

**The operator** — whoever deploys and configures the agent — is trusted. So is
the machine it runs on and the environment variables it is given.

In a TRE, only its administrators may configure the Agent and its policy. The Agent trusts this administrator-controlled policy; a researcher, workflow or coordinator cannot replace it with rules supplied in a job.

## Keys and envelopes

Trust in a job rests on keys, not on the connection it arrived over.

- The node has two keys that never leave it: an approval key that signs its
  decisions and results, and an encryption key that jobs are encrypted for.
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

## Outputs

Outputs leave a node only one way. Each output is checked against the study's
output rules by the Policy Engine; files the rules hold back never leave. The
rest go to the TRE's airlock, or wait for the data manager to release or
withhold them in the agent's page. Only released outputs are encrypted for the
result recipients, signed with the node key and sent to the coordinator. Rules
that need a file's contents and cannot be checked are marked "not verified",
and that marking travels with the release decision.

## Boundaries

| boundary | what crosses it | what has to hold |
|---|---|---|
| coordinator → agent, polled by the agent over mTLS | study versions with the members' keys and ID tokens, and jobs signed by a member and encrypted for this node | authenticate both ends; verify the member's signature, the approved study version and workflow, and the visas, then authorize the task locally; validate names, inputs and shell-bound values |
| TRE engine → TES API | one command in one image, with files in and out | require a token, and TLS off loopback; run it through the same runner and its guarantees; validate names, inputs and shell-bound values |
| identity evidence → agent | members' LS Login ID tokens and GA4GH visas | check each token's issuer keys, audience, subject, nonce and expiry; check each visa's issuer, value, age and that it belongs to the member; apply local access rules |
| registry → agent | the signed catalogue, bundle metadata, tool binaries | verify the catalogue, immutable artifact identity and required evidence, and check local tool approval before execution; still treat content as untrusted |
| agent → tool | a working directory, argv, local data access | confine filesystem, process privileges, and network access to the approved run |
| tool → agent | files in that directory | read as data, never followed out of the run |
| agent → coordinator | results signed by the node and encrypted for the result recipients | apply the output rules and the TRE's release before any data leaves; authorize the destination and record the transfer |

That last row is the one worth dwelling on: the result sent to the coordinator
is the data-exit path. Anything that lets a tool cause
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
  outside it. A run directory alone cannot confine filesystem, process, and
  network access; that has to come from the runner and the deployment.
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
  run's outputs, logs and manifest by run id (**#90**). Once callers are
  distinguishable, that is access across projects on a shared Agent.

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

## Applying it

When judging whether something is a defect, ask:

1. Can a **caller or coordinator** cause it with a task, workflow, or input?
2. Can a **tool** cause it with the access its configured runner grants?
3. Does it end with data crossing the Agent's result or egress boundary, or
   with the Agent's execution being redirected?

A "yes" to 2 counts. That is the correction this document exists to record.

## Implementation status

What the code does today, so that nobody counts on a control that does not
exist yet.

| Control | State | Where |
|---|---|---|
| Until polling lands, the agent listens on an inbound HTTP port and `BIOCHEF_AUTH` defaults to `none`. Meanwhile a site must set `BIOCHEF_AUTH` to `bearer` or `passport` and restrict who can reach the port | Gap (on master) | `main.py`, `auth.py` |
| Agent polls the coordinator over mTLS, with no inbound port | Not started | #106, work plan part 8; waits for the agent API in biochef-coordinator |
| Signed catalogue, required by default | On master | #100 |
| Pulled bytes checked against manifest digests | On master | #9 |
| Tool signature and evidence verification | On master, off by default (`BIOCHEF_SIGNING_MODE=off`) | #14 |
| GA4GH visas, each verified against its own issuer | On master | #79 |
| Node keys, and signed decisions written to the audit trail | On master | #109 |
| Data manager's page, behind the TRE's login | In PR | #110 |
| Study and workflow approval, signed with the node key | In PR | #112 |
| Member ID token check, nonce bound to the keys | In PR | #114 |
| Dataset bindings with their visa value | In PR | #115 |
| Request checks: member signature, node, approved study and workflow, expiry, replay, decryption, digest, visas per dataset | In PR | #116 |
| Output rules (the agent's own check), held outputs, airlock or manual release, results signed and encrypted for the recipients | In PR | #117 |
| Audit check on the node page | In PR | #118 |
| Signed node list from the federation | In PR | #119 |
| Each step in a container of its own with no network | In PR | #120 (apptainer), #121 (podman), #123 (singularity); on master the default runner is `subprocess` and apptainer is optional (#15) |
| Refusing a runner without network isolation, and the check at start | In PR | #122 |
| TES runner and TES API | In PR | #124, #125 |
| Policy Engine | Not started | protocol and fake in #112 and #117 |
| Connection to a TRE's airlock | Not started | protocol and fake in #117; the interface is TRE specific |
| Posting decisions and results to the coordinator | Not started | #106, work plan part 8 |
| Authorization between callers | Not started | #90 |
| Bounds on disk use, workflow size and output size | Not started | #91 |
| Bundle-supplied strings quoted before the shell | Not started | #42 |

Updated each time a control lands on master.
