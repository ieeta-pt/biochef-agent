"""A GA4GH TES API on a node, so a TRE's own workflow engines can send it tasks.

SPEC.md, section 14.4. The agent runs steps of the workflows a federation asks it
for. This lets something inside the TRE ask it for one task at a time instead,
through the interface such engines already speak, without a second way to run code
on the node. A task is a command in an image, and it runs with the same runner and
the same guarantees as a federated step.

It is a listener of its own (`python -m tesapi`), not routes on the agent's API:
the API's callers are federations and editors, these are the TRE's own tools, and
the two should not share a credential, a queue, a slot or a port. What it adds is
refusal: every request is authenticated, only listed images run, only listed
directories are read or written and only through file://, and it never opens the
node's database or the directories the studies' datasets live in.
"""
