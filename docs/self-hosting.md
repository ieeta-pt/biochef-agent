# Running BioChef locally

A registry, the agent, and the editor, on one machine, from a clean checkout.

This is a **development** stack. It runs a registry over plain HTTP with no
authentication and an agent with no authentication in front of it, which is fine
on a laptop and is not a deployment. What it is for is having the three pieces
talk to each other so you can change one and see the effect.

## What you need

- Docker with Compose v2 (`docker compose version` should print `v2.x`)
- The editor checked out beside this repository

```
git clone https://github.com/ieeta-pt/biochef-agent.git
git clone https://github.com/ieeta-pt/Biochef.git
```

so that the two directories sit side by side:

```
.
├── biochef-agent      # you are here
└── Biochef            # the editor
```

If the editor is somewhere else, set `FRONTEND_CONTEXT` to its path.

## Bringing it up

From `biochef-agent`:

```
docker compose up --build
```

The first run takes a few minutes: it installs the agent's pinned Python
dependencies and the editor's npm packages. Later runs reuse both.

When it has settled you have:

| | |
|---|---|
| the editor | <http://localhost:3000> |
| the agent | <http://localhost:8000/docs> |
| the registry | <http://localhost:5000/v2/> |

To check without a browser:

```
./ci/stack_smoke.sh
```

which is the same thing CI runs, and asserts each service actually answers
rather than that the containers were created.

## The one piece of ordering that matters

The agent builds its registry client and logs in **at import**. It does not
start and then retry: if the registry is not answering, the agent exits.

The compose file handles this with a healthcheck on the registry and
`depends_on: condition: service_healthy`, so `up` is deterministic. It is worth
knowing because it is the first thing to suspect if you adapt this file and the
agent starts failing "randomly" — almost always it is starting before whatever
it pulls from.

## Publishing a tool to your local registry

The stack comes up empty. The agent pulls tool bundles from the registry by the
`repo` named in a workflow, so until something is published, a workflow that
names a tool will fail at the pull with a 404.

Publishing is [biochef-hub](https://github.com/ieeta-pt/biochef-hub)'s job, and
its own documentation covers building a recipe. Point it at this registry with:

```
REGISTRY_URL=localhost:5000
REGISTRY_INSECURE=true
```

## Changing things

| what you want | how |
|---|---|
| a different port | `AGENT_PORT=9000 docker compose up` — also `REGISTRY_PORT`, `FRONTEND_PORT` |
| the editor elsewhere | `FRONTEND_CONTEXT=/path/to/Biochef docker compose up` |
| agent logs | `docker compose logs -f agent` |
| a shell in the agent | `docker compose exec agent sh` |
| start over | `docker compose down -v` — the `-v` also drops the registry's contents |

Editing the editor's source takes effect without a restart; it is mounted, and
the dev server reloads. Editing the agent's source does not — rebuild it with
`docker compose up --build agent`.

## What this stack does not do

- **No authentication anywhere.** The registry accepts anonymous pushes and the
  agent answers anyone who can reach the port.
- **No isolation.** The agent executes tool binaries pulled from the registry
  inside its own container, as an unprivileged user but with no sandbox of its
  own.
- **Plain HTTP**, so nothing here is safe across a network you do not control.

Do not put this on an address other people can reach.
