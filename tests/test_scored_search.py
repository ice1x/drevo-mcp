"""Tests for the scored-search tools (``vector_search`` / ``fts_search``).

Two layers, mirroring the rest of the suite:

- **server layer** — monkeypatch ``server.kg`` with a stub and assert the tool
  returns the ``[{"node", "score"}]`` JSON shape, and that a failure still
  becomes a structured error envelope via ``_guard``. No live server needed.
- **integration** — opt-in against a live drevo Bolt server (``DREVO_BOLT_URL``),
  exercising drevo's ``CALL fts.search`` / ``CALL drevo.vector.query``
  procedures end to end. Skips unless the server is reachable, so offline CI
  stays green.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
from faker import Faker
from neo4j.exceptions import ServiceUnavailable

from drevo_mcp_bolt import server
from drevo_mcp_bolt.graph import KnowledgeGraph

# ── Server layer (mocked kg, always runs) ─────────────────────────────


def test_vector_search_returns_scored_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def vector_search(
            self, label: str, prop: str, query: list[float], k: int
        ) -> list[dict[str, Any]]:
            assert (label, prop, query, k) == ("Doc", "embedding", [0.1, 0.2], 3)
            return [{"node": {"name": "a"}, "score": 0.98}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.vector_search("Doc", "embedding", [0.1, 0.2], 3)))
    assert out == [{"node": {"name": "a"}, "score": 0.98}]


def test_fts_search_returns_scored_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def fts_search(self, query: str, k: int) -> list[dict[str, Any]]:
            assert (query, k) == ("anxious thoughts", 5)
            return [{"node": {"name": "a"}, "score": 4.2}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.fts_search("anxious thoughts", 5)))
    assert out == [{"node": {"name": "a"}, "score": 4.2}]


def test_scored_search_default_k(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    class _KG:
        async def fts_search(self, query: str, k: int) -> list[dict[str, Any]]:
            seen["k"] = k
            return []

    monkeypatch.setattr(server, "kg", _KG())
    asyncio.run(server.fts_search("q"))
    assert seen["k"] == 10  # default surfaced to the graph layer


def test_fts_search_failure_becomes_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def fts_search(self, query: str, k: int) -> list[dict[str, Any]]:
            raise ServiceUnavailable("down")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.fts_search("q", 5)))
    assert out["ok"] is False
    assert out["error_type"] == "unavailable"


# ── Integration (opt-in, live drevo) ──────────────────────────────────

_BOLT_URL = os.environ.get("DREVO_BOLT_URL")
_BOLT_USER = os.environ.get("DREVO_BOLT_USER", "neo4j")
_BOLT_PASS = os.environ.get("DREVO_BOLT_PASSWORD", "drevo")


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), 0.5):
            return True
    except OSError:
        return False


_RUN = _BOLT_URL is not None and _reachable(_BOLT_URL)


async def _exercise(url: str) -> None:
    fake = Faker()
    project = f"it-{uuid.uuid4().hex[:12]}"
    # A distinctive single token so BM25 matches exactly this node and nothing
    # from the surrounding graph; the rest of the text is realistic (faker).
    token = f"zqx{uuid.uuid4().hex[:10]}"
    name = f"{fake.word()}-{token}"
    observation = f"{fake.sentence()} mentioning {token}"

    kg = KnowledgeGraph(uri=url, username=_BOLT_USER, password=_BOLT_PASS)
    await kg.connect()
    try:
        await kg.create_entity(name, "Note", project, [observation])

        # fts.search finds the node by its distinctive token, scored.
        hits = await kg.fts_search(token, 10)
        assert any(h["node"].get("name") == name for h in hits), hits
        assert all(isinstance(h["score"], (int, float)) for h in hits)

        # vector.query over a label with no embedding property: empty result,
        # but the procedure must be registered (no error).
        empty = await kg.vector_search("Note", "embedding", [0.1, 0.2, 0.3], 3)
        assert empty == []
    finally:
        await kg.run_cypher("MATCH (e:Entity {project: $p}) DETACH DELETE e", {"p": project})
        await kg.close()


@pytest.mark.skipif(not _RUN, reason="set DREVO_BOLT_URL to a reachable drevo Bolt server to run")
def test_scored_search_over_drevo_bolt() -> None:
    assert _BOLT_URL is not None  # narrowed by the skipif guard
    asyncio.run(_exercise(_BOLT_URL))
