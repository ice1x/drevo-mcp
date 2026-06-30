# drevo-mcp-bolt

[![CI](https://github.com/ice1x/drevo-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ice1x/drevo-mcp/actions/workflows/ci.yml)

A self-contained **knowledge-graph MCP server** that exposes a running **drevo**
graph database to AI clients (Claude Code, Claude Desktop, OpenCode, Cline, …) as
[Model Context Protocol](https://modelcontextprotocol.io) tools — **with full
read *and* write access**.

It is a **Bolt drop-in of the Neo4j knowledge-graph MCP**: the same tools and the
same Cypher, but pointed at drevo's **Neo4j-compatible Bolt endpoint** instead of
Neo4j. `drevo-server` speaks Bolt (the official `neo4j` driver accepts it) and the
Cypher subset these tools use (`MERGE` / `datetime()` / `SET +=` / map projection
/ `labels()` / `type()` / `properties()` / `OPTIONAL MATCH` / `collect`), so it is
a genuine copy-and-swap.

```
MCP client ──stdio(MCP)──▶ drevo-mcp-bolt (this repo) ──Bolt (neo4j driver)──▶ drevo-server :7687 ──▶ drevo.redb
```

Unlike a plain HTTP wrapper, this MCP **mutates the graph**: it can create and
delete entities and relationships, append observations, record and apply schema
migrations, and run arbitrary Cypher. One process owns the redb file (the
container); this MCP is just a Bolt client.

This repo is **self-contained**: it ships the Python MCP, a `docker-compose.yml`
and a `scripts/run-drevo.sh` helper that pull and start the published
[`ice1x/drevo`](https://hub.docker.com/r/ice1x/drevo) image **with Bolt enabled**,
and the client configuration snippets below.

The one drevo difference from real Neo4j: `CREATE INDEX` schema DDL is unsupported
(drevo auto-indexes), so index creation is best-effort and a no-op on drevo.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — start the drevo container (Bolt enabled)](#step-1--start-the-drevo-container-bolt-enabled)
3. [Step 2 — install this MCP server](#step-2--install-this-mcp-server)
4. [Step 3 — verify the wire](#step-3--verify-the-wire)
5. [Step 4 — connect an MCP client](#step-4--connect-an-mcp-client)
   - [Claude Code](#claude-code)
   - [OpenCode](#opencode)
   - [Cline (VS Code)](#cline-vs-code)
   - [Claude Desktop](#claude-desktop)
6. [Tools](#tools)
7. [Using it from a chat](#using-it-from-a-chat)
8. [The data model (entities / relationships / projects)](#the-data-model-entities--relationships--projects)
9. [Configuration reference](#configuration-reference)
10. [Develop / test](#develop--test)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Docker** (to run the `drevo-server` container), and
- **Python ≥ 3.13** (to run this MCP server).

The MCP server is a normal Python process that an AI client spawns over stdio;
the database is a separate container it reaches over **Bolt** (port 7687).

---

## Step 1 — start the drevo container (Bolt enabled)

The MCP server needs a running `drevo-server` **with its Bolt listener open**. The
Bolt listener is opt-in: the server only opens 7687 when `DREVO_BOLT_PORT` is set.
The compose file and helper script in this repo set it for you. The image lives on
Docker Hub: <https://hub.docker.com/r/ice1x/drevo>. Pick **any one** way below.

### Option A — helper script (simplest)

```bash
./scripts/run-drevo.sh          # pulls ice1x/drevo:latest, enables Bolt, waits for /health
```

It pulls the image, bind-mounts `./data` for the redb file, runs the container as
your host user, sets `DREVO_BOLT_PORT=7687`, and blocks until `GET /health` is
green. Other sub-commands:

```bash
./scripts/run-drevo.sh logs     # follow container logs
./scripts/run-drevo.sh stop     # stop & remove the container (host data kept)
```

Override defaults with env vars, e.g.:

```bash
DREVO_TAG=0.1.0 DREVO_PORT=9090 DREVO_BOLT_PORT=7687 DREVO_DATA_DIR=~/drevo_data ./scripts/run-drevo.sh
```

### Option B — docker compose

```bash
mkdir -p ./data
DREVO_UID=$(id -u) DREVO_GID=$(id -g) docker compose up -d
docker compose logs -f          # watch it boot
docker compose down             # stop later (host data dir is left untouched)
```

The compose file sets `DREVO_BOLT_PORT=7687` and publishes it. `docker compose
pull` refreshes to the newest `latest`.

### Option C — plain `docker run`

```bash
mkdir -p ./data
docker run -d --name drevo \
  -p 8080:8080 -p 7687:7687 \
  --user "$(id -u):$(id -g)" \
  -e DREVO_HOST=0.0.0.0 -e DREVO_PORT=8080 -e DREVO_BOLT_PORT=7687 -e DREVO_DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  ice1x/drevo:latest
```

### Confirm it is up (any option)

```bash
curl localhost:8080/health      # {"status":"ok"}
nc -z localhost 7687 && echo "bolt open"   # the Bolt listener must be open
open http://localhost:8080/ui   # interactive graph Web UI (macOS; use your browser elsewhere)
```

What the container exposes:

| Port  | Purpose                                            |
|-------|----------------------------------------------------|
| 8080  | HTTP API **and** the embedded Web UI (`/ui`)       |
| 7687  | **Bolt (Neo4j-compatible) — what this MCP uses**   |

The redb database file is persisted on the **host** at `./data/drevo.redb` (or
wherever `DREVO_DATA_DIR` points), so it survives `down`/`stop`.

---

## Step 2 — install this MCP server

Install the package into a Python environment. A virtualenv is recommended so the
AI client can launch a known interpreter:

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e .                          # from this repo root
```

Note the **absolute path** to that interpreter — you will point the MCP client at
it so it does not depend on `PATH`:

```bash
python -c "import sys; print(sys.executable)"
# e.g. /Users/you/repo/drevo-mcp/.venv/bin/python
```

---

## Step 3 — verify the wire

Smoke-test the MCP protocol without any client — pipe three JSON-RPC lines in and
watch the tool list come back:

```bash
export DREVO_BOLT_URL=bolt://localhost:7687   # default; override if elsewhere
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python -m drevo_mcp_bolt
```

You should see a JSON-RPC response listing `create_entity`, `search_knowledge`,
`run_cypher`, etc. If you do, the server and the container are talking over Bolt.

drevo's Bolt runs **without authentication**, so the username / password are
accepted and ignored — they only matter against a real Neo4j.

---

## Step 4 — connect an MCP client

All clients launch the **same command** — `python -m drevo_mcp_bolt` — and pass the
target server via the `DREVO_BOLT_URL` environment variable. Use the **absolute
path** to your venv's `python` (from Step 2) as the command to avoid `PATH`
surprises; below it is written as `/abs/path/to/.venv/bin/python`.

### Claude Code

Easiest is the CLI (run it from anywhere):

```bash
claude mcp add drevo \
  --env DREVO_BOLT_URL=bolt://localhost:7687 \
  -- /abs/path/to/.venv/bin/python -m drevo_mcp_bolt
```

Add `--scope project` to write a shareable `.mcp.json` into the current repo
instead of your user config. That file looks like:

```json
{
  "mcpServers": {
    "drevo": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "drevo_mcp_bolt"],
      "env": { "DREVO_BOLT_URL": "bolt://localhost:7687" }
    }
  }
}
```

Verify inside Claude Code with `/mcp` — `drevo` should be listed as connected.

### OpenCode

OpenCode reads `opencode.json` (project root) or `~/.config/opencode/opencode.json`.
MCP servers go under the `mcp` key as a **local** (stdio) server:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "drevo": {
      "type": "local",
      "command": ["/abs/path/to/.venv/bin/python", "-m", "drevo_mcp_bolt"],
      "enabled": true,
      "environment": { "DREVO_BOLT_URL": "bolt://localhost:7687" }
    }
  }
}
```

Note OpenCode's spelling: the program + args are a single `command` array, and the
env block is `environment` (not `env`).

### Cline (VS Code)

Open Cline → **MCP Servers** → **Configure MCP Servers**, which opens
`cline_mcp_settings.json`. Add:

```json
{
  "mcpServers": {
    "drevo": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "drevo_mcp_bolt"],
      "env": { "DREVO_BOLT_URL": "bolt://localhost:7687" },
      "disabled": false,
      "autoApprove": ["get_entity", "search_knowledge", "get_project_graph",
                      "list_projects", "get_migrations"]
    }
  }
}
```

Unlike the read-only HTTP MCP, **these tools mutate the graph** — `create_entity`,
`delete_entity`, `create_relationship`, `delete_relationship`, `apply_migration`
and `run_cypher` can change or remove data. Keep those **out** of `autoApprove`
(as above, only the read tools are listed) so each write asks for a confirmation.

### Claude Desktop

Edit `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`) and add the same
`mcpServers` block shown for Claude Code, then restart the app.

---

## Tools

The server exposes thirteen tools — **read, write, and migration**. The JSON
Schema for each is generated automatically by FastMCP from the function
signatures, so clients discover arguments via `tools/list`.

### Entities (write)

| Tool | Arguments | Effect |
|------|-----------|--------|
| `create_entity` | `name`, `entity_type`, `project`, `observations=None`, `properties=None` | Create or merge an entity (`MERGE` on `name`+`project`). |
| `add_observations` | `name`, `project`, `observations` | Append observations to an existing entity. |
| `delete_entity` | `name`, `project` | Delete an entity and all its relationships (`DETACH DELETE`). |

### Relationships (write)

| Tool | Arguments | Effect |
|------|-----------|--------|
| `create_relationship` | `from_entity`, `to_entity`, `relation_type`, `project`, `properties=None` | Create a typed relationship between two entities. |
| `delete_relationship` | `from_entity`, `to_entity`, `relation_type`, `project` | Delete a specific relationship. |

### Queries (read)

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_entity` | `name`, `project` | The entity with its incoming and outgoing relationships. |
| `search_knowledge` | `query`, `project=None` | Entities matching `query` in name or observations. |
| `get_project_graph` | `project` | The full entity/relationship graph for a project. |
| `list_projects` | — | All distinct project namespaces. |

### Migrations (write)

| Tool | Arguments | Effect |
|------|-----------|--------|
| `add_migration` | `project`, `description`, `cypher_up`, `cypher_down=None`, `version=None` | Record a schema/data migration (not yet applied). |
| `get_migrations` | `project` | The migration history for a project. |
| `apply_migration` | `project`, `seq` | Execute a pending migration's `cypher_up` and mark it applied. |

### Raw Cypher (read **or** write)

| Tool | Arguments | Effect |
|------|-----------|--------|
| `run_cypher` | `query`, `params=None` | Execute an arbitrary Cypher query — can read or mutate. |

---

## Using it from a chat

Once connected, just ask the assistant in natural language — it picks the tools:

- *"Add a `service` entity called `billing` to project `erp`."* → `create_entity(name="billing", entity_type="service", project="erp")`
- *"Note that billing now depends on payments."* → `create_relationship("billing", "payments", "DEPENDS_ON", "erp")`
- *"What do we know about billing in erp?"* → `get_entity("billing", "erp")`
- *"Search the erp graph for anything about invoices."* → `search_knowledge("invoice", "erp")`
- *"Show me the whole erp project graph."* → `get_project_graph("erp")`
- *"Remove the billing→payments dependency."* → `delete_relationship("billing", "payments", "DEPENDS_ON", "erp")`

A reliable pattern: **`create_entity` for the nodes → `create_relationship` to
link them → `get_project_graph` / `search_knowledge` to read back.**

---

## The data model (entities / relationships / projects)

This MCP models a **project-scoped knowledge graph** (the Neo4j knowledge-graph
shape), which is a thin layer over drevo's property graph:

- An **entity** is a node labelled `Entity` with a `name`, a `type`, a list of
  `observations` (free-text facts), arbitrary `properties`, and a `project`
  namespace. Entities are unique per `(name, project)`.
- A **relationship** is a typed, directed edge between two entities in the same
  project (e.g. `DEPENDS_ON`, `KNOWS`, `PART_OF`). Relationship types are
  sanitised to upper-snake-case.
- A **project** is just the `project` property — every tool takes it so multiple
  knowledge graphs can live in one drevo instance without colliding. Discover the
  ones that exist with `list_projects`.
- A **migration** is a `Migration` node recording a `cypher_up` / `cypher_down`
  pair, sequenced per project, that `apply_migration` executes on demand.

You generally pick your own entity/relationship types per scenario, for example:

| Scenario          | Example entity types                       | Example relationship types          |
|-------------------|--------------------------------------------|-------------------------------------|
| IT task manager   | `task`, `person`, `project`, `sprint`      | `ASSIGNED_TO`, `BLOCKS`, `PART_OF`  |
| Bug tracker       | `bug`, `component`, `release`, `person`    | `AFFECTS`, `FIXED_IN`, `REPORTED_BY`|
| Story / book editor | `chapter`, `scene`, `character`, `place` | `APPEARS_IN`, `PRECEDES`, `SET_IN`  |
| CBT journal       | `entry`, `thought`, `emotion`, `distortion`| `TRIGGERS`, `REFRAMES`, `TAGGED`    |
| ERP               | `order`, `invoice`, `product`, `customer`  | `CONTAINS`, `BILLED_TO`, `SUPPLIES` |

For anything the tool surface doesn't cover directly, `run_cypher` runs arbitrary
Cypher against the same graph.

---

## Configuration reference

This MCP server reads these environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DREVO_BOLT_URL` | `bolt://localhost:7687` | Bolt URI of the running `drevo-server`. |
| `DREVO_BOLT_USER` | `neo4j` | Username (accepted and ignored by drevo). |
| `DREVO_BOLT_PASSWORD` | `drevo` | Password (accepted and ignored by drevo). |
| `DREVO_BOLT_DATABASE` | `neo4j` | Bolt database name. |

The container (Step 1) reads these, mirrored by the compose file and the helper
script:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DREVO_TAG` | `latest` | Image tag to pull (`latest`, `0.1.0`, …). |
| `DREVO_PORT` | `8080` | Host port mapped to the container's HTTP API. |
| `DREVO_BOLT_PORT` | `7687` | Host port mapped to the Bolt endpoint **and** the env var that opens the listener. |
| `DREVO_DATA_DIR` | `./data` | Host folder bind-mounted to `/data` (holds `drevo.redb`). |
| `DREVO_UID` / `DREVO_GID` | `1000` | UID/GID the container runs as (set to `$(id -u)`/`$(id -g)`). |

---

## Develop / test

```bash
pip install -e ".[dev]"
pytest                       # unit tests run offline (no live server needed)
mypy --strict drevo_mcp_bolt/
ruff check . && black --check .
```

The unit tests run offline. The end-to-end test in `tests/test_integration.py` is
**opt-in**: it drives a real Bolt server and is skipped unless `DREVO_BOLT_URL`
is set **and** the port is open. To run it against the container from Step 1:

```bash
DREVO_BOLT_URL=bolt://localhost:7687 pytest -q tests/test_integration.py
```

It writes only into a throwaway `it-…` project namespace and deletes everything
it creates.

---

## Troubleshooting

- **Tool calls fail with a connection error** — the container isn't up, Bolt isn't
  enabled, or `DREVO_BOLT_URL` is wrong. Check `nc -z localhost 7687`; if it is
  closed, the server was started without `DREVO_BOLT_PORT` (use the compose file /
  helper script in this repo, which set it).
- **Client shows the server as "failed to start"** — the `command` likely isn't
  the interpreter where this package is installed. Use the absolute path to your
  venv's `python` (Step 2).
- **`CREATE INDEX` errors in logs** — harmless: drevo auto-indexes and rejects
  schema DDL, so index creation is best-effort and ignored.
- **Permission denied writing `drevo.redb`** — the container user can't write the
  bind-mounted folder. Start it as your host user (`--user $(id -u):$(id -g)`,
  which the script and compose file already do).

---

## License

Dual-licensed under MIT or Apache-2.0. See [LICENSE](LICENSE).
