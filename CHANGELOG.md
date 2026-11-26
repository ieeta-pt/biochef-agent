# Changelog

Changes that matter to someone running the agent. What an existing deployment has
to do about them is in `UPGRADING.md`.

## Unreleased

### Changed

- **Network isolation is required unless it is turned off.** `BIOCHEF_REQUIRE_NETWORK_ISOLATION` is on when unset, and the agent and the page refuse to start with a runner that does not itself keep its steps off the network. That includes `subprocess`, the default runner, and `apptainer` with `--net --network none` removed from `BIOCHEF_APPTAINER_ARGS`. An agent installed before this change stops at start until its configuration chooses. Setting `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false` is explicit, and is recorded in the audit trail at every start as `node.runner.isolation_not_required`. See `UPGRADING.md`.

### Added

- **What each runner guarantees:** for every step, network, filesystem and environment, each `enforced`, `attested` or `none`, shown on the Node screen.
- **A network check at start:** a runner whose network guarantee is `enforced` runs one step when the API process starts. The step must see no route or address besides loopback, and, with `BIOCHEF_NETWORK_PROBE=host:port`, must not reach a host the agent itself can. A failed check stops the agent. The result is audited as `node.runner.network_check` and, on a node, shown on the Node screen.
- **Migration 0005** adds the check's result to `node_status`.
