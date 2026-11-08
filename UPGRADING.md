# Upgrading

What an agent that is already installed has to change when it is updated, and
why. The changes themselves are in `CHANGELOG.md`.

## Network isolation is now required unless it is turned off

### What you will see

After the update, the agent, and the page if it runs, stop at start with:

```
BIOCHEF_REQUIRE_NETWORK_ISOLATION, on unless set to false, refuses BIOCHEF_RUNNER=subprocess: the agent does not itself keep its steps off the network (network isolation is none), ...
```

This happens when the runner is `subprocess`, which is the default when `BIOCHEF_RUNNER` is unset, or `apptainer` with `--net --network none` removed from `BIOCHEF_APPTAINER_ARGS`. An agent whose runner already isolates the network starts as before.

### Why

A step that can reach the network can send the data it read anywhere, and keeping data in is the reason a TRE exists. Before this change an agent ran such steps unless someone asked otherwise. Now it refuses unless someone says, in its configuration, that it should.

### What to do

Choose one:

1. **Use a runner that isolates the network.**
   - `apptainer` or `singularity`, with the default `BIOCHEF_APPTAINER_ARGS` and `BIOCHEF_APPTAINER_BINARY` (next section).
   - `podman`, rootless or rootful.

   See "Running it" and "What each runner guarantees" in the README.
2. **Turn the rule off, deliberately,** with `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false`: for development, or for a host with no container runtime where steps may run with the network. The agent then starts as before. At every start, the API process records `node.runner.isolation_not_required` in the audit trail, with the runner it let in, so the choice stays visible.

### Also at start

- A runner whose network isolation is enforced now runs one step when the API process starts, to show that the step has no network. If the step finds a route or an address besides loopback, or cannot read its routes, the agent does not start, and the audit trail says why (`node.runner.network_check`). With `BIOCHEF_NETWORK_PROBE=host:port`, the step also tries to reach a host the agent itself can reach.
- On a node, run `python -m store migrate` after updating, for migration 0005, and for 0006 if you run the TES API (`python -m tesapi` stops at start against a database without it).

## Apptainer needs the path of its binary

### What you will see

An agent with `BIOCHEF_RUNNER=apptainer` stops at start with:

```
BIOCHEF_APPTAINER_BINARY='': BIOCHEF_RUNNER=apptainer needs the absolute path of the apptainer binary, and it has no default
```

### Why

Snakemake has no setting for the binary. It runs `apptainer` if one is on `PATH`, and `singularity` otherwise. Apptainer's packages also install a `singularity` that is apptainer. So which program ran each step depended on `PATH`. The agent now names the binary, checks what it says it is, and puts a link to it first on Snakemake's `PATH`.

### What to do

Set `BIOCHEF_APPTAINER_BINARY` to the absolute path of the binary, for example the output of `command -v apptainer`. The agent refuses to start if the binary does not say `apptainer version`. For SingularityCE, use `BIOCHEF_RUNNER=singularity`, whose binary must say `singularity-ce version`.
