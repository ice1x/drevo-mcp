"""Keeps the README honest: it must document every client and every tool.

This repo's value is largely the setup docs, so we lock the two things most
likely to silently drift: the list of supported MCP clients, and the set of
tool names (which must match what the server actually registers).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import drevo_mcp_bolt.server as server

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"


def _registered_tool_names() -> set[str]:
    tools = asyncio.run(server.mcp.list_tools())
    return {tool.name for tool in tools}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_documents_each_client() -> None:
    text = _readme()
    for client in ("Claude Code", "OpenCode", "Cline", "Claude Desktop"):
        assert client in text, f"README must document the {client} client"


def test_readme_lists_every_tool() -> None:
    text = _readme()
    for tool in _registered_tool_names():
        assert f"`{tool}`" in text, f"README must document the '{tool}' tool"


def test_readme_documents_bolt_connection() -> None:
    # The MCP talks Bolt, so the README must point clients at DREVO_BOLT_URL.
    assert "DREVO_BOLT_URL" in _readme(), "README must document the Bolt connection URL"


def test_readme_points_at_docker_hub_image() -> None:
    assert "hub.docker.com/r/ice1x/drevo" in _readme(), "README must link the Docker Hub image"
