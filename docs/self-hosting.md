# Running the agent locally

Two ways to run the agent on one machine:

- **This repository's stack:** a registry, the agent and the editor, with one `docker compose up`. Use it to work on the agent itself.
- **The local TRE:** `make up` in [biochef-infra](https://github.com/ieeta-pt/biochef-infra) runs the whole platform with simulated silos, and builds the agent from this repository's `Dockerfile`. Its docs are in biochef-infra's `docs/local-build.md`.

Both are development setups: plain HTTP, no real data, bound to `127.0.0.1`.

## How the agent talks

In BioChef nothing connects to an agent. The editor submits jobs to the coordinator, and each agent, inside its silo, polls the coordinator for them. The agent opens every connection: to the coordinator, and to a registry for tools.

The coordinator's side of this is not built yet, so for now you call the agent directly, from the same machine (below).

The agent will also have a frontend of its own: the agent page, where a node's data manager approves studies and workflows, follows runs and releases outputs. It is a separate process on its own port, reachable only inside the TRE, and it is not in this stack yet. It is a different thing from the editor, which researchers use.

## This repository's stack

You need Docker with Compose v2, and the editor checked out beside this repository, as the biochef-infra submodules are:

```
.
├── biochef-agent      # you are here
└── biochef-app        # the editor
```

From `biochef-agent`:

```
docker compose up --build
```

| | |
|---|---|
| the editor | <http://localhost:3000> |
| the agent | <http://localhost:8000/docs> |
| the registry | <http://localhost:5000/v2/> |

`./ci/stack_smoke.sh` checks that each one answers; CI runs the same script.

The registry starts empty, so a workflow that names a tool fails at the pull until the tool is published there. Publish with [biochef-hub](https://github.com/ieeta-pt/biochef-hub), pointed at `REGISTRY_URL=localhost:5000` and `REGISTRY_INSECURE=true`. Because this registry has no signed catalogue, the compose file sets `BIOCHEF_TOOL_CATALOG=off`.

## Calling the agent

Open <http://localhost:8000/docs>, or post a workflow and its inputs:

```
curl -F "biochef_workflow=<workflow.json" -F files=@input.fasta \
     http://localhost:8000/convert -o results.zip
```

Use `<` (the workflow is a form field) and keep the quotes, or the agent answers 422.

## The agent in the local TRE

In `make up`, each silo runs this image. What it needs there:

| | |
|---|---|
| Network | An internal network with no published port. Its only ways out are the coordinator and, under avenue A, the global registry, both through the silo's egress proxy |
| Tools | `REGISTRY_URL` names the global registry (avenue A) or the silo's own registry (avenue B), picked in the silo's `.env`. `REGISTRY_INSECURE=true`, because the local registries are plain HTTP |
| Catalogue | Left at `required`. The local registries are seeded from production, signed catalogue included |
| Credentials | `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` must not be empty, even for a registry without authentication, or the agent stops at start |
| Data | `BIOCHEF_DATA_SOURCES` includes `localpath`, and `BIOCHEF_LOCAL_ROOT` is the silo's synthetic data store |
| Start order | The registry must answer before the agent starts: the agent logs in to it at start and exits if it cannot |
| Who calls it | Only the silo's shell (`make shell`), until the agent polls the coordinator |
| Agent page | Once it exists, it runs next to the agent on its own port, reachable from inside the silo only, never published |
| Steps | In the agent's own container for now; the container is the boundary on a laptop |

The image runs as an unprivileged user (uid 10001), and `/app` is writable by it, because the agent writes uploads and tool bundles under its working directory.

## Changing this stack

| What you want | How |
|---|---|
| other ports | `AGENT_PORT=9000 docker compose up`, also `REGISTRY_PORT` and `FRONTEND_PORT` |
| the editor elsewhere | `FRONTEND_CONTEXT=/path/to/biochef-app docker compose up` |
| agent logs | `docker compose logs -f agent` |
| a shell in the agent | `docker compose exec agent sh` |
| your agent changes | `docker compose up --build agent`; editor changes reload on their own |
| start over | `docker compose down -v`, which also empties the registry |

Every port binds to `127.0.0.1`. `BIND_HOST=0.0.0.0` makes the stack reachable from your network, where anyone can call the agent and push to the registry, so use it only on a network you trust.
