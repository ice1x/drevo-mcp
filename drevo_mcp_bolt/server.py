"""MCP server exposing a project knowledge graph backed by drevo over Bolt.

A Bolt drop-in of the Neo4j knowledge-graph MCP: identical tools and Cypher,
but pointed at drevo's Neo4j-compatible Bolt endpoint (a containerised
`drevo-server` with `DREVO_BOLT_PORT` set). Connect a Neo4j instance or a drevo
container — the tool surface is the same.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from drevo_mcp_bolt.graph import EmbeddingError, KnowledgeGraph, KnowledgeGraphError

# ── Configuration ─────────────────────────────────────────────────────
# Point at drevo's Bolt endpoint by default (the container sets
# DREVO_BOLT_PORT=7687). drevo's Bolt runs without auth, so the username /
# password are accepted and ignored — they only matter against real Neo4j.

_BOLT_URI = os.getenv("DREVO_BOLT_URL", "bolt://localhost:7687")
_BOLT_USER = os.getenv("DREVO_BOLT_USER", "neo4j")
_BOLT_PASS = os.getenv("DREVO_BOLT_PASSWORD", "drevo")
_BOLT_DB = os.getenv("DREVO_BOLT_DATABASE", "neo4j")
# drevo's HTTP API base — used by `semantic_search` to reach the OpenAI-compatible
# `/v1/embeddings` endpoint (issue #217). Separate from Bolt because embedding
# generation is HTTP-only in drevo.
_HTTP_URL = os.getenv("DREVO_HTTP_URL", "http://localhost:8080")

kg = KnowledgeGraph(
    uri=_BOLT_URI,
    username=_BOLT_USER,
    password=_BOLT_PASS,
    database=_BOLT_DB,
    http_url=_HTTP_URL,
)


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    await kg.connect()
    try:
        yield
    finally:
        await kg.close()


mcp = FastMCP(
    "drevo-knowledge-graph",
    instructions=(
        "Knowledge Graph MCP — store and query project knowledge, domain "
        "models, and schema migrations in drevo over Bolt (Neo4j-compatible)."
    ),
    lifespan=lifespan,
)


def _json(obj: Any) -> str:
    """Serialise arbitrary Bolt results to JSON."""

    def default(o: Any) -> Any:
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)

    return json.dumps(obj, indent=2, default=default, ensure_ascii=False)


def _error(message: str, error_type: str) -> str:
    """A structured, machine-readable error envelope returned by every tool."""
    return _json({"ok": False, "error": message, "error_type": error_type})


_T = TypeVar("_T", bound=Callable[..., Awaitable[str]])


def _guard(func: _T) -> _T:
    """Turn failures into a clean JSON envelope instead of a raw exception.

    Without this, a stopped drevo container (or a malformed Cypher query) raises
    a driver exception that FastMCP surfaces as an ``isError`` tool result with
    an opaque message. Here each failure becomes
    ``{"ok": false, "error": ..., "error_type": ...}`` so a client/LLM can react:

    - ``not_found``       — the targeted entity/relationship does not exist
    - ``embedding_error`` — text-to-vector embedding failed (drevo /v1/embeddings
      unreachable, not configured (503), or a non-float response)
    - ``unavailable``     — the graph is unreachable (container down, network)
    - ``query_error``     — the server rejected the query (bad Cypher, constraint)
    - ``not_connected``   — the graph was never connected
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except EmbeddingError as exc:
            # Must precede KnowledgeGraphError — EmbeddingError subclasses it.
            return _error(str(exc), "embedding_error")
        except KnowledgeGraphError as exc:
            return _error(str(exc), "not_found")
        except (ServiceUnavailable, SessionExpired) as exc:
            return _error(f"knowledge graph unavailable: {exc}", "unavailable")
        except Neo4jError as exc:
            return _error(f"query failed: {exc}", "query_error")
        except RuntimeError as exc:
            return _error(str(exc), "not_connected")

    return wrapper  # type: ignore[return-value]


# ── Tools: Entities ───────────────────────────────────────────────────


@mcp.tool()
@_guard
async def create_entity(
    name: str,
    entity_type: str,
    project: str,
    observations: list[str] | None = None,
    properties: dict[str, Any] | None = None,
) -> str:
    """Create or update a knowledge entity in the graph."""
    result = await kg.create_entity(name, entity_type, project, observations, properties)
    return _json(result)


@mcp.tool()
@_guard
async def add_observations(name: str, project: str, observations: list[str]) -> str:
    """Append new observations to an existing entity."""
    result = await kg.add_observations(name, project, observations)
    return _json(result)


@mcp.tool()
@_guard
async def delete_entity(name: str, project: str) -> str:
    """Delete an entity and all its relationships."""
    deleted = await kg.delete_entity(name, project)
    return _json({"deleted": deleted})


# ── Tools: Relationships ─────────────────────────────────────────────


@mcp.tool()
@_guard
async def create_relationship(
    from_entity: str,
    to_entity: str,
    relation_type: str,
    project: str,
    properties: dict[str, Any] | None = None,
) -> str:
    """Create a typed relationship between two entities."""
    result = await kg.create_relationship(
        from_entity, to_entity, relation_type, project, properties
    )
    return _json(result)


@mcp.tool()
@_guard
async def delete_relationship(
    from_entity: str, to_entity: str, relation_type: str, project: str
) -> str:
    """Delete a relationship between two entities."""
    deleted = await kg.delete_relationship(from_entity, to_entity, relation_type, project)
    return _json({"deleted": deleted})


# ── Tools: Queries ────────────────────────────────────────────────────


@mcp.tool()
@_guard
async def get_entity(name: str, project: str) -> str:
    """Get an entity with all its incoming and outgoing relationships."""
    result = await kg.get_entity(name, project)
    return _json(result)


@mcp.tool()
@_guard
async def search_knowledge(query: str, project: str | None = None) -> str:
    """Search the knowledge graph by text (entity names and observations)."""
    results = await kg.search(query, project)
    return _json(results)


@mcp.tool()
@_guard
async def get_project_graph(project: str) -> str:
    """Get the complete knowledge graph for a project."""
    result = await kg.get_project_graph(project)
    return _json(result)


@mcp.tool()
@_guard
async def list_projects() -> str:
    """List all projects stored in the knowledge graph."""
    projects = await kg.list_projects()
    return _json(projects)


# ── Tools: Migrations ────────────────────────────────────────────────


@mcp.tool()
@_guard
async def add_migration(
    project: str,
    description: str,
    cypher_up: str,
    cypher_down: str | None = None,
    version: str | None = None,
) -> str:
    """Record a graph schema/data migration for a project."""
    result = await kg.add_migration(project, description, cypher_up, cypher_down, version)
    return _json(result)


@mcp.tool()
@_guard
async def get_migrations(project: str) -> str:
    """Get the full migration history for a project."""
    results = await kg.get_migrations(project)
    return _json(results)


@mcp.tool()
@_guard
async def apply_migration(project: str, seq: int) -> str:
    """Execute a pending migration and mark it as applied."""
    result = await kg.apply_migration(project, seq)
    return _json(result)


# ── Tools: Raw Cypher ─────────────────────────────────────────────────


@mcp.tool()
@_guard
async def run_cypher(query: str, params: dict[str, Any] | None = None) -> str:
    """Execute a Cypher query against the knowledge graph."""
    results = await kg.run_cypher(query, params)
    return _json(results)


# ── Tools: Scored search ──────────────────────────────────────────────


@mcp.tool()
@_guard
async def vector_search(label: str, prop: str, query: list[float], k: int = 10) -> str:
    """Scored vector search: the top-`k` nodes nearest to an embedding.

    Wraps drevo's `CALL drevo.vector.query`. `label` / `prop` select the node
    label and its embedding property, `query` is the query vector, `k` the
    number of neighbours. Returns `[{"node": {...}, "score": float}]`,
    ordered best-first — first-class scored retrieval without hand-writing
    Cypher.
    """
    results = await kg.vector_search(label, prop, query, k)
    return _json(results)


@mcp.tool()
@_guard
async def fts_search(query: str, k: int = 10) -> str:
    """Scored full-text search: the top-`k` nodes matching the query text (BM25).

    Wraps drevo's `CALL fts.search`. `query` is the search text, `k` the number
    of results. Returns `[{"node": {...}, "score": float}]`, ordered best-first.
    """
    results = await kg.fts_search(query, k)
    return _json(results)


@mcp.tool()
@_guard
async def semantic_search(
    query: str,
    label: str,
    prop: str = "embedding",
    k: int = 10,
    model: str | None = None,
    fallback_to_fts: bool = True,
) -> str:
    """Semantic search: embed `query` with drevo's own `/v1/embeddings`, then
    return the top-`k` nodes nearest that vector under `label`.`prop`.

    Text-in, ranked-nodes-out — the self-contained RAG path where one drevo
    instance provides graph, vectors, and embedding generation. `prop` is the
    embedding property (default `embedding`); `model` is optional (drevo fills
    its configured default when omitted). Returns `[{"node": {...}, "score":
    float}]`, best-first.

    Works without an LLM: if embeddings are not configured (drevo answers 503)
    or the upstream errors, and `fallback_to_fts` is set (the default), this
    transparently degrades to `fts_search` — lexical BM25 over the query text —
    so you still get relevant nodes. The fallback searches indexed node text
    (title/body) graph-wide, not `label`.`prop` embeddings. Set
    `fallback_to_fts=false` to get an `embedding_error` instead.
    """
    results = await kg.semantic_search(query, label, prop, k, model, fallback_to_fts)
    return _json(results)


# ── Entrypoint ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="drevo Knowledge Graph MCP Server (Bolt)")
    parser.add_argument("--db-url", default=_BOLT_URI, help="drevo Bolt connection URI")
    parser.add_argument("--username", default=_BOLT_USER)
    parser.add_argument("--password", default=_BOLT_PASS)
    parser.add_argument("--database", default=_BOLT_DB)
    parser.add_argument(
        "--http-url", default=_HTTP_URL, help="drevo HTTP base for /v1/embeddings (semantic_search)"
    )
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    args = parser.parse_args()

    kg.uri = args.db_url
    kg.username = args.username
    kg.password = args.password
    kg.database = args.database
    kg.http_url = args.http_url

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
