# Changelog

Changes that matter to someone running the agent. What an existing deployment has
to do about them is in `UPGRADING.md`.

## Unreleased

### Changed

- **Network isolation is required unless it is turned off.** `BIOCHEF_REQUIRE_NETWORK_ISOLATION` is on when unset, and the agent and the page refuse to start with a runner that does not itself keep its steps off the network. That includes `subprocess`, the default runner, and `apptainer` with `--net --network none` removed from `BIOCHEF_APPTAINER_ARGS`. An agent installed before this change stops at start until its configuration chooses. Setting `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false` is explicit, and is recorded in the audit trail at every start as `node.runner.isolation_not_required`. See `UPGRADING.md`.

- **`apptainer` needs `BIOCHEF_APPTAINER_BINARY`,** the absolute path of the binary, with no default. Snakemake runs whichever `apptainer` or `singularity` it finds first, so the agent names it, checks what it says it is, and refuses to start without it. See `UPGRADING.md`.

### Added

- **A `tes` runner:** `BIOCHEF_RUNNER=tes` sends each step to a GA4GH TES server that already exists, such as Funnel, through the `tes-step` script. The image is only ever given by digest, the task holds no environment and no secret, and the files that come back are checked by SHA-256 before they are placed. `tes-step-root`, called through one sudo rule, gives the files of a server running as root back to the agent. Its network and filesystem guarantees are `attested` from `BIOCHEF_TES_ISOLATION`, so `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false` is needed to use it. See the README. Tested in CI against a stand-in and against Funnel v0.12.2 built from its tag's commit; Funnel mounts inputs read-only, so the step runs in a copy of the workspace.

- **What each runner guarantees:** for every step, network, filesystem and environment, each `enforced`, `attested` or `none`, shown on the Node screen.
- **A network check at start:** a runner whose network guarantee is `enforced` runs one step when the API process starts. The step must see no route or address besides loopback, and, with `BIOCHEF_NETWORK_PROBE=host:port`, must not reach a host the agent itself can. A failed check stops the agent. The result is audited as `node.runner.network_check` and, on a node, shown on the Node screen.
- **Migration 0005** adds the check's result to `node_status`.
- **A `singularity` runner** for SingularityCE, with the apptainer runner's flags and guarantees.
