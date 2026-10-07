# Threat model

What this service assumes about who is trusted, so that a change can be argued
about on the same terms twice running.

This document describes the model, not a claim that every boundary is enforced.
Which parts are enforced changes with every merge; what the agent is *for* does
not. Check the source and deployment configuration before treating a control
as active. Gaps are named with the issue that tracks them.

The current `/convert` prototype accepts inbound requests and returns results
to its caller. The accepted TRE deployment is an outbound-only Agent that polls
a coordinator, exposes no external port, and checks local policy before sending
any output. The same trust questions apply to both flows, but the destination
and authorization decision move to the Agent-to-coordinator boundary in the
target deployment.

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

The TRE defines which tools may run and who may access each dataset. For each job, the Agent checks the researcher's identity and data access, and whether the selected tool is approved. It runs the job only if local rules permit it, and checks permission to release results to the intended recipient.

The TRE must provide trusted policy and configuration, approved data-access permissions, and an execution environment that isolates jobs and enforces CPU, memory, disk and process limits. It also defines retention rules, protects audit records and provides the local output-release procedure. Execution limits must hold at the host, storage or scheduler, not only in the workflow description.

The Agent must use those controls when admitting and running a job. A workflow or coordinator cannot override local rules. If required permission, artifact verification or execution protections cannot be established, the Agent must refuse the affected execution or release.

## Actors

**The researcher** is the named person who approves a job. The Agent must check their identity and local permission to use the requested data and tools. It is important to consider that an authenticated researcher can still submit a hostile job.

**The caller or coordinator** supplies a workflow and its inputs. Untrusted as a
source of tasks even after its identity is established. The coordinator may relay only jobs the researcher explicitly approved while  the Agent must verify that approval rather than accept the coordinator's word. Approval for one job does not permit additional or changed jobs.

In the target deployment, the Agent must authenticate the coordinator and authorize each task against local data and execution policy; the coordinator must also authenticate the Agent. Authenticating the coordinator does not establish the researcher's identity or permissions. Audit records must identify the researcher who approved each job.

**The identity provider** performs login and issues evidence the Agent checks for a trusted issuer, the intended recipient and validity. LS-AAI/OIDC is the intended goal and institutional providers are possible additional deployment profiles. The site decides which providers and claims it trusts. Identity checks do not by themselves prove job approval or data access.

**The workflow description** is task-supplied data. Its node ids, parameter
values, and edge handles may reach a generated Snakefile or a filename, so they
must be validated rather than trusted as a description.

**The tool bundle** comes from the registry, and the task selects which one:
the `repo` for each node is a field in the request. The Agent now checks pulled
bytes against manifest digests (**#9**) and offers signature and evidence
verification in `strict` mode (**#14**). That mode is not the default; a TRE
deployment must enable it with an appropriate local policy. Verification does
not prove that a correctly signed tool is safe. Bundle-supplied strings remain
untrusted input (**#42**).

The TRE decides which tools may run under its local policy. Before execution, the Agent must check that policy permits the selected tool artifact for the job.

**The tool binary, once running, is untrusted.** This is the one most easily got
wrong. It is arbitrary compiled code, fetched from a registry. With the default
subprocess runner it executes with the Agent's privileges against whatever the
deployment can see. The optional container runner changes that access but does
not itself establish complete confinement. A tool can read, write, and spawn
processes wherever its runtime permits. "The attacker is already executing
code on the host" is not a precondition to be argued away here; it is Tuesday.

Tools must receive only the data and resources approved for their run. They must not inherit the Agent's credentials or access its configuration and policy.

**The data** is the asset. In the deployments this is written for it is the
reason the environment exists, and the reason it may not leave.
Results, intermediate files and logs can also be sensitive and must remain inside the TRE until local policy approves release and Agent audit records must not contain raw research data, access tokens or secrets.

**The operator** — whoever deploys and configures the agent — is trusted. So is
the machine it runs on and the environment variables it is given.

In a TRE, only its administrators may configure the Agent and its policy. The Agent trusts this administrator-controlled policy; a researcher, workflow or coordinator cannot replace it with rules supplied in a job.

## Boundaries

| boundary | what crosses it | what has to hold |
|---|---|---|
| caller/coordinator → agent | workflow JSON, uploaded files or data references, researcher identity and job approval | authenticate the source; verify researcher identity and approval, then authorize the task locally; validate names, inputs and shell-bound values |
| identity-provider evidence → agent | researcher identity and authorization claims | check the site-accepted issuer, intended recipient and validity; apply local access rules |
| registry → agent | bundle metadata, tool binaries | verify immutable artifact identity and required evidence, and check local tool approval before execution; still treat content as untrusted |
| agent → tool | a working directory, argv, local data access | confine filesystem, process privileges, and network access to the approved run |
| tool → agent | files in that directory | read as data, never followed out of the run |
| agent → caller/coordinator | response body or outbound result transmission | apply local output policy before any data leaves; authorize the destination and record the transfer |

That last row is the one worth dwelling on. In the current inbound prototype,
the response body is a data-exit path. In the outbound Agent, result transmission
to the coordinator is the corresponding path. Anything that lets a tool cause
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
  run's outputs, logs and manifest by run id (**#90**). Under a shared bearer
  token that is unavoidable; once callers are distinguishable (#79) it is access
  across projects on a shared Agent.

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
