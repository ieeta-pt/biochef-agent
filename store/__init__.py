"""What a node remembers about the federation (docs/agent-ui/SPEC.md, section 6).

The agent's runs live in the memory of one process and are gone after a restart,
which is fine for "submit, poll, collect" and not for this. An approval is a
signed promise that has to outlast the process that made it, and the page that
shows it is a second process. So this package is the one place those facts are
kept, and the only channel between the two processes.

Importing it opens nothing. The API does not need a database to run a workflow,
and must not start needing one because a module got imported.
"""
