"""Always-on unit checks (no Bolt server needed).

These lock the wiring that is easy to break silently: the Bolt defaults point
at the drevo container's Bolt port, the graph object refuses use before
``connect()``, and the MCP registers exactly the write/query/migration tool
surface the README documents.
"""

from __future__ import annotations

import asyncio

import pytest

import drevo_mcp_bolt.server as server
from drevo_mcp_bolt.graph import KnowledgeGraph

# The full tool surface the server exposes. Kept here (not derived from the
# module) so an accidental rename or a dropped ``@mcp.tool()`` fails loudly.
EXPECTED_TOOLS = {
    "create_entity",
    "add_observations",
    "delete_entity",
    "create_relationship",
    "delete_relationship",
    "get_entity",
    "search_knowledge",
    "get_project_graph",
    "list_projects",
    "add_migration",
    "get_migrations",
    "apply_migration",
    "run_cypher",
    "vector_search",
    "fts_search",
    "semantic_search",
    "hybrid_search",
    "add_message",
    "get_conversation",
    "recall_memory",
    "record_reasoning",
    "remember_entity",
    "assert_fact",
    "retract_fact",
    "facts_at",
    "stable_matching",
}


def _registered_tool_names() -> set[str]:
    tools = asyncio.run(server.mcp.list_tools())
    return {tool.name for tool in tools}


def test_drv_raises_when_not_connected() -> None:
    kg = KnowledgeGraph(uri="bolt://x", username="u", password="p")
    with pytest.raises(RuntimeError):
        _ = kg._drv


def test_server_default_uri_targets_drevo_bolt_port() -> None:
    # Defaults point at the drevo container's Bolt port, not a generic Neo4j.
    assert server._BOLT_URI.endswith(":7687")
    assert server.kg.uri.endswith(":7687")


def test_default_database_is_drevo_not_neo4j() -> None:
    # drevo's default database is named "drevo" (its protected DEFAULT_DB), not
    # Neo4j's conventional "neo4j". Since drevo's multi-database catalog landed
    # it rejects `USE neo4j` with a 404, so a Bolt driver that falls back to
    # database="neo4j" fails every call with "database `neo4j` does not exist".
    # The client must therefore default to "drevo" when DREVO_BOLT_DATABASE is
    # unset — both on the dataclass field and the server's env fallback literal.
    assert KnowledgeGraph(uri="bolt://x", username="u", password="p").database == "drevo"
    assert server.kg.database == "drevo"


def test_server_registers_full_tool_surface() -> None:
    assert _registered_tool_names() == EXPECTED_TOOLS
