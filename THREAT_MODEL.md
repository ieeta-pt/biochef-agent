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

## Actors

**The caller or coordinator** supplies a workflow and its inputs. Untrusted as a
source of tasks even after its identity is established. The current prototype
has no caller authentication (**#10**), Passport-based identity (**#22**), or
local authorization for what a caller may run or read. In the target deployment,
the Agent must authenticate the coordinator and authorize each task against
local data and execution policy; the coordinator must also authenticate the
Agent.

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

**The tool binary, once running, is untrusted.** This is the one most easily got
wrong. It is arbitrary compiled code, fetched from a registry. With the default
subprocess runner it executes with the Agent's privileges against whatever the
deployment can see. The optional container runner changes that access but does
not itself establish complete confinement. A tool can read, write, and spawn
processes wherever its runtime permits. "The attacker is already executing
code on the host" is not a precondition to be argued away here; it is Tuesday.

**The data** is the asset. In the deployments this is written for it is the
reason the environment exists, and the reason it may not leave.

**The operator** — whoever deploys and configures the agent — is trusted. So is
the machine it runs on and the environment variables it is given.

## Boundaries

| boundary | what crosses it | what has to hold |
|---|---|---|
| caller/coordinator → agent | workflow JSON, uploaded files or data references | authenticate and authorize the source and task; validate names, inputs and shell-bound values |
| registry → agent | bundle metadata, tool binaries | verify immutable artifact identity and required evidence before execution; still treat content as untrusted |
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

## Not defended against

Stated plainly, because a threat model that implies more coverage than it has is
worse than none.

- **A malicious tool escaping its working directory.** Snakemake's `--directory`
  sets an origin, not a jail: a rule whose output is `../escaped` can write
  outside it. A container runner now exists (**#15**), but the default runner is
  still a subprocess and this document does not establish that a deployment
  confines all filesystem, process, and network access. A run directory alone
  cannot do that.
- **A malicious but correctly signed tool.** Digest and signature checks can
  bind the bytes to an identity and policy; they cannot establish benign
  behavior or rule out a compromised publisher.
- **Resource exhaustion.** Request-body middleware now enforces a configurable
  upload-byte limit (**#11**). That does not supply a concurrency cap, disk
  quota, or complete runtime and output limits.
- **Anything requiring the operator to be hostile.** Someone who can set the
  environment or write to the tool cache has already won, and defending against
  that is out of scope.

## Applying it

When judging whether something is a defect, ask:

1. Can a **caller or coordinator** cause it with a task, workflow, or input?
2. Can a **tool** cause it with the access its configured runner grants?
3. Does it end with data crossing the Agent's result or egress boundary, or
   with the Agent's execution being redirected?

A "yes" to 2 counts. That is the correction this document exists to record.
