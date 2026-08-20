# Threat model

What this service assumes about who is trusted, so that a change can be argued
about on the same terms twice running.

This document describes the model, not the current state of enforcement. Which
parts are enforced changes with every merge; what the agent is *for* does not.
Gaps are named with the issue that tracks them.

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

**The caller** submits a workflow and its inputs. Untrusted. Today nothing
authenticates them at all (**#10**), and nothing decides what a given caller may
run or read (**#22**, GA4GH Passports). Until both exist, the caller is whoever
can reach the port.

**The workflow description** is caller-supplied data. Every value in it — node
ids, parameter values, edge handles — reaches either a generated Snakefile or a
filename, so all of it is input to be validated rather than a description to be
trusted.

**The tool bundle** comes from the registry, and *the caller chooses which one*:
the `repo` for each node is a field in the request. Nothing verifies that what
arrives is what the catalogue intended — no digest check (**#9**) and no
signature verification (**#14**) — so bundle-supplied strings are no more
trustworthy than caller-supplied ones (**#42**).

**The tool binary, once running, is untrusted.** This is the one most easily got
wrong. It is arbitrary compiled code, fetched from a registry, executing with
the agent's privileges against whatever the deployment can see. It shares a uid
with the agent, it can read and write the filesystem, and it can spawn
processes. "The attacker is already executing code on the host" is not a
precondition to be argued away here; it is Tuesday.

**The data** is the asset. In the deployments this is written for it is the
reason the environment exists, and the reason it may not leave.

**The operator** — whoever deploys and configures the agent — is trusted. So is
the machine it runs on and the environment variables it is given.

## Boundaries

| boundary | what crosses it | what has to hold |
|---|---|---|
| caller → agent | workflow JSON, uploaded files | names are checked for shape and against what the workflow declares; values reach a shell only quoted |
| registry → agent | bundle metadata, tool binaries | treated as untrusted input; ideally verified before use (#9, #14) |
| agent → tool | a working directory, argv | the tool gets a directory of its own and nothing outside it |
| tool → agent | files in that directory | read as data, never followed out of the run |
| agent → caller | the response body | **this is how data leaves**, and it is the boundary that matters most |

That last row is the one worth dwelling on. In a TRE the response body is the
exfiltration path. Anything that lets a tool cause the agent to read a file it
did not produce — a symbolic link, a hard link, an output slot pre-filled by an
upload — turns a legitimate API response into a way out. Findings of that shape
are not "the caller deceiving itself"; they are the thing the environment exists
to prevent.

## Not defended against

Stated plainly, because a threat model that implies more coverage than it has is
worse than none.

- **A malicious tool escaping its working directory.** Snakemake's `--directory`
  sets an origin, not a jail: a rule whose output is `../escaped` writes outside
  it and snakemake exits 0. Confining a running tool needs a container or an
  equivalent (**#15**). Until then the agent constrains what a *request* can
  name, not what a *process* can reach.
- **A compromised registry**, until #9 and #14 exist.
- **Resource exhaustion.** There is no upload size limit, no concurrency cap and
  no disk quota. The multipart body is spooled before the handler is entered, so
  a size limit belongs in middleware or a proxy rather than in the handler
  (**#11**).
- **Anything requiring the operator to be hostile.** Someone who can set the
  environment or write to the tool cache has already won, and defending against
  that is out of scope.

## Applying it

When judging whether something is a defect, the question is not "could a remote
client do this" but:

1. Can a **caller** cause it, with a workflow and some uploads?
2. Can a **tool** cause it, being arbitrary code with the agent's privileges?
3. Does it end with data crossing the agent → caller boundary, or with the
   agent's own execution being redirected?

A "yes" to 2 counts. That is the correction this document exists to record.
