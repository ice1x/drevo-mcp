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


def test_server_registers_full_tool_surface() -> None:
    assert _registered_tool_names() == EXPECTED_TOOLS
