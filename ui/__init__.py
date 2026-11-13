"""The page a node's data manager uses inside a TRE (docs/agent-ui/SPEC.md).

A second ASGI app, on its own port. It never mounts the API's routes and the API
never serves it, because the two authenticate different people in different ways
and the audit trail must not be reachable through the API it audits.
"""
