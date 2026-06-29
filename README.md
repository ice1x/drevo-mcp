# drevo-mcp

[![CI](https://github.com/ice1x/drevo-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ice1x/drevo-mcp/actions/workflows/ci.yml)

A self-contained **[FastMCP](https://github.com/jlowin/fastmcp) server** that exposes a
running **drevo** graph database to AI clients (Claude Code, Claude Desktop,
OpenCode, Cline, …) as [Model Context Protocol](https://modelcontextprotocol.io)
tools.

It talks to drevo **over HTTP** and **never opens the redb file directly**, so it
does not fight the server for redb's single-process file lock — the container
owns the file, this process is just an HTTP client. Every tool maps to one
endpoint you can also hit with `curl`, which makes it trivial to debug.

```
MCP client  ──stdio (MCP)──▶  drevo-mcp (this repo)  ──HTTP──▶  drevo-server (container)  ──▶  drevo.redb
```

This repo is **self-contained**: it ships the Python MCP, a `docker-compose.yml`
and a `scripts/run-drevo.sh` helper that pull and start the published
[`ice1x/drevo`](https://hub.docker.com/r/ice1x/drevo) image, and the client
configuration snippets below.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — start the drevo container](#step-1--start-the-drevo-container)
3. [Step 2 — install this MCP server](#step-2--install-this-mcp-server)
4. [Step 3 — verify the wire](#step-3--verify-the-wire)
5. [Step 4 — connect an MCP client](#step-4--connect-an-mcp-client)
   - [Claude Code](#claude-code)
   - [OpenCode](#opencode)
   - [Cline (VS Code)](#cline-vs-code)
   - [Claude Desktop](#claude-desktop)
6. [Tools](#tools-read-only)
7. [Using it from a chat](#using-it-from-a-chat)
8. [Understanding the schema (node/edge kinds)](#understanding-the-schema-nodeedge-kinds)
9. [Configuration reference](#configuration-reference)
10. [Develop / test](#develop--test)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Docker** (to run the `drevo-server` container), and
- **Python ≥ 3.10** (to run this MCP server).

The MCP server is a normal Python process that an AI client spawns over stdio;
the database is a separate container it reaches over HTTP.

---

## Step 1 — start the drevo container

The MCP server needs a running `drevo-server`. The image lives on Docker Hub:
<https://hub.docker.com/r/ice1x/drevo>. Pick **any one** of the three ways below.

### Option A — helper script (simplest)

```bash
./scripts/run-drevo.sh          # pulls ice1x/drevo:latest, starts it, waits for /health
```

It pulls the image, bind-mounts `./data` for the redb file, runs the container as
your host user, and blocks until `GET /health` is green. Other sub-commands:

```bash
./scripts/run-drevo.sh logs     # follow container logs
./scripts/run-drevo.sh stop     # stop & remove the container (host data kept)
```

Override defaults with env vars, e.g.:

```bash
DREVO_TAG=0.1.0 DREVO_PORT=9090 DREVO_DATA_DIR=~/drevo_data ./scripts/run-drevo.sh
```

### Option B — docker compose

```bash
mkdir -p ./data
DREVO_UID=$(id -u) DREVO_GID=$(id -g) docker compose up -d
docker compose logs -f          # watch it boot
docker compose down             # stop later (host data dir is left untouched)
```

`docker compose pull` refreshes to the newest `latest`.

### Option C — plain `docker run`

```bash
mkdir -p ./data
docker run -d --name drevo \
  -p 8080:8080 -p 7687:7687 \
  --user "$(id -u):$(id -g)" \
  -e DREVO_HOST=0.0.0.0 -e DREVO_PORT=8080 -e DREVO_DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  ice1x/drevo:latest
```

### Confirm it is up (any option)

```bash
curl localhost:8080/health      # {"status":"ok"}
open http://localhost:8080/ui   # interactive graph Web UI (macOS; use your browser elsewhere)
```

What the container exposes:

| Port  | Purpose                                   |
|-------|-------------------------------------------|
| 8080  | HTTP API **and** the embedded Web UI (`/ui`) |
| 7687  | Bolt (Neo4j-compatible) — not used by this MCP |

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
export DREVO_HTTP_URL=http://localhost:8080   # default; override if elsewhere
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python -m drevo_mcp
```

You should see a JSON-RPC response listing `health`, `node_get`, `search_fts`, etc.
If you do, the server and the container are talking.

---

## Step 4 — connect an MCP client

All clients launch the **same command** — `python -m drevo_mcp` — and pass the
target server via the `DREVO_HTTP_URL` environment variable. Use the **absolute
path** to your venv's `python` (from Step 2) as the command to avoid `PATH`
surprises; below it is written as `/abs/path/to/.venv/bin/python`.

### Claude Code

Easiest is the CLI (run it from anywhere):

```bash
claude mcp add drevo \
  --env DREVO_HTTP_URL=http://localhost:8080 \
  -- /abs/path/to/.venv/bin/python -m drevo_mcp
```

Add `--scope project` to write a shareable `.mcp.json` into the current repo
instead of your user config. That file looks like:

```json
{
  "mcpServers": {
    "drevo": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "drevo_mcp"],
      "env": { "DREVO_HTTP_URL": "http://localhost:8080" }
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
      "command": ["/abs/path/to/.venv/bin/python", "-m", "drevo_mcp"],
      "enabled": true,
      "environment": { "DREVO_HTTP_URL": "http://localhost:8080" }
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
      "args": ["-m", "drevo_mcp"],
      "env": { "DREVO_HTTP_URL": "http://localhost:8080" },
      "disabled": false,
      "autoApprove": ["health", "node_get", "list_nodes_by_kind", "search_fts",
                      "neighbors", "subgraph", "shortest_path", "count_nodes"]
    }
  }
}
```

Every tool here is **read-only**, so listing them all in `autoApprove` is safe and
saves you a confirmation click per call.

### Claude Desktop

Edit `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`) and add the same
`mcpServers` block shown for Claude Code, then restart the app.

---

## Tools (read-only)

The server exposes eight read-only tools. The JSON Schema for each is generated
automatically by FastMCP from the function signatures, so clients discover
arguments via `tools/list` — you do not declare schemas anywhere.

| Tool | Arguments | drevo endpoint | Returns |
|------|-----------|----------------|---------|
| `health` | — | `GET /health` | `{"status":"ok"}` |
| `node_get` | `node_id` | `GET /nodes/{id}` | the node, or `null` if absent |
| `list_nodes_by_kind` | `kind`, `limit=50`, `offset=0` | `GET /nodes?kind=` | `{"nodes":[…]}` |
| `search_fts` | `query`, `limit=10` | `POST /search/fts` | `{"results":[{"node":…,"score":…}]}` |
| `neighbors` | `node_id`, `direction="both"`, `kind=None`, `depth=1` | `GET /nodes/{id}/neighbors` | `{"nodes":[…]}` |
| `subgraph` | `node_id`, `depth=1` | `GET /nodes/{id}/subgraph` | `{"nodes":[…],"edges":[…]}` |
| `shortest_path` | `from_id`, `to_id` | `GET /paths/shortest` | `{"path":[ids] \| null}` |
| `count_nodes` | — | `GET /export/json` | `{"count":n}` |

`direction` is `"outgoing"` \| `"incoming"` \| `"both"`. `count_nodes` downloads
the full export and counts it (drevo has no dedicated count endpoint yet), so it is
fine for modest graphs.

---

## Using it from a chat

Once connected, just ask the assistant in natural language — it picks the tools:

- *"Is the drevo server healthy?"* → `health`
- *"Find nodes mentioning 'invoice' and show me the top 5."* → `search_fts(query="invoice", limit=5)`
- *"Show node 42 and its direct neighbours."* → `node_get(42)` then `neighbors(42, depth=1)`
- *"List the first 20 `task` nodes."* → `list_nodes_by_kind(kind="task", limit=20)`
- *"Is there a path from node 3 to node 91?"* → `shortest_path(3, 91)`
- *"Give me the 2-hop subgraph around node 7."* → `subgraph(7, depth=2)`

A reliable pattern when you don't know ids yet: **`search_fts` to find an entry
node → `node_get` to read it → `neighbors`/`subgraph` to expand.**

---

## Understanding the schema (node/edge kinds)

drevo is a **property graph**. Each **node** has a numeric `id`, a `kind` (its
label/type, e.g. `task`, `person`, `chapter`), a `title`/`body`, and properties.
Each **edge** also has a `kind` (e.g. `depends_on`, `wrote`, `mentions`).

There are two distinct "schemas" worth separating:

1. **The MCP tool schema** — the argument shape of each tool. You do **not**
   configure this; FastMCP derives it from the Python type hints and the client
   fetches it via `tools/list`. Nothing to do.

2. **The graph schema** — *which* `kind` values exist in your data. This is what
   you usually mean by "the right schema", and it depends entirely on what you
   loaded into drevo. It matters because `list_nodes_by_kind` (and the server's
   `/nodes?kind=` / `/facets?kind=`) **require** a `kind` — you must name a kind
   that actually exists, or you get an empty/400 result.

### How to discover the kinds that exist

drevo has no "list all kinds" endpoint, so discover them from the data:

- **Web UI** — open `http://localhost:8080/ui` and look at the rendered graph;
  node/edge kinds are visible there.
- **Search first** — `search_fts("<a word you expect>")`, then `node_get(<id>)` on
  a hit; the returned object's `kind` field tells you the label to reuse with
  `list_nodes_by_kind`.
- **Export** — `curl localhost:8080/export/json` dumps every node and edge; the
  distinct `kind` values are your schema. (`count_nodes` uses this same dump.)

### Telling the assistant the schema

The cleanest way to get good queries is to **state the kinds up front** — in the
client's system prompt, a project rules file, or just the first chat message — so
the model uses real labels instead of guessing. For example, for the scenarios
drevo targets you might tell it:

| Scenario          | Example node kinds                         | Example edge kinds              |
|-------------------|--------------------------------------------|---------------------------------|
| IT task manager   | `task`, `person`, `project`, `sprint`      | `assigned_to`, `blocks`, `part_of` |
| Bug tracker       | `bug`, `component`, `release`, `person`    | `affects`, `fixed_in`, `reported_by` |
| Story / book editor | `chapter`, `scene`, `character`, `place` | `appears_in`, `precedes`, `set_in` |
| CBT journal       | `entry`, `thought`, `emotion`, `distortion` | `triggers`, `reframes`, `tagged` |
| ERP               | `order`, `invoice`, `product`, `customer`  | `contains`, `billed_to`, `supplies` |

These are **illustrative** — replace them with the kinds your data actually uses
(discover them as above). Once the model knows the kinds, `list_nodes_by_kind`,
`neighbors(kind=…)`, and faceting all "just work".

---

## Configuration reference

This MCP server reads a single environment variable:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DREVO_HTTP_URL` | `http://localhost:8080` | Base URL of the running `drevo-server`. |

The container (Step 1) reads these, mirrored by the compose file and the helper
script:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DREVO_TAG` | `latest` | Image tag to pull (`latest`, `0.1.0`, …). |
| `DREVO_PORT` | `8080` | Host port mapped to the container's HTTP API. |
| `DREVO_BOLT_PORT` | `7687` | Host port mapped to the Bolt endpoint. |
| `DREVO_DATA_DIR` | `./data` | Host folder bind-mounted to `/data` (holds `drevo.redb`). |
| `DREVO_UID` / `DREVO_GID` | `1000` | UID/GID the container runs as (set to `$(id -u)`/`$(id -g)`). |

---

## Develop / test

```bash
pip install -e ".[dev]"
pytest                       # unit tests (HTTP is mocked — no live server needed)
mypy --strict drevo_mcp/
ruff check . && black --check .
```

The test suite mocks the `httpx` transport, so `pytest` runs offline; only the
end-to-end smoke test in [Step 3](#step-3--verify-the-wire) needs a live
container.

---

## Troubleshooting

- **Tool calls fail with a connection error** — the container isn't up or
  `DREVO_HTTP_URL` is wrong. Check `curl localhost:8080/health` and that the URL
  matches the host port you published.
- **Client shows the server as "failed to start"** — the `command` likely isn't
  the interpreter where `drevo-mcp` is installed. Use the absolute path to your
  venv's `python` (Step 2).
- **`list_nodes_by_kind` returns nothing** — you passed a `kind` that doesn't
  exist. Discover the real kinds (see [the schema section](#understanding-the-schema-nodeedge-kinds)).
- **Permission denied writing `drevo.redb`** — the container user can't write the
  bind-mounted folder. Start it as your host user (`--user $(id -u):$(id -g)`,
  which the script and compose file already do).

---

## License

Dual-licensed under MIT or Apache-2.0. See [LICENSE](LICENSE).
