"""Tests for `graph_search` — graph-aware retrieval ("retrieve then expand").

`graph_search` is what makes this a *graph* RAG rather than one more vector DB:
it relevance-ranks seed nodes with `hybrid_search`, then walks the graph outward
`hops` steps and returns the seeds plus their deduplicated neighbourhood, so an
LLM gets both the most-relevant nodes and their connected context.

Multi-hop expansion is a **client-side BFS** over single-hop `neighbors` calls —
deliberately not a `[*1..n]` variable-length path, which is not in drevo's
documented Cypher subset. The single-hop query reuses `get_entity`'s exact,
drevo-proven idiom (directed `OPTIONAL MATCH` + `type()` + map projection).

Layers, mirroring the rest of the suite:

- **pure core** — `_expandable`: a node can seed further expansion only when it
  has a `(name, project)` identity.
- **graph layer** — `KnowledgeGraph.graph_search` composition with
  `hybrid_search` / `neighbors` monkeypatched: one- and two-hop walks, dedup of
  seeds and shared neighbours, and identity-less nodes that cannot expand.
- **server layer** — the `graph_search` MCP tool with a stub `kg`, asserting the
  JSON shape and that a failure becomes a structured error envelope.
- **integration** — opt-in against a live drevo Bolt server, exercising the real
  `neighbors` Cypher and the end-to-end retrieve-then-expand path.
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
from drevo_mcp_bolt.graph import KnowledgeGraph, _expandable


def _kg() -> KnowledgeGraph:
    return KnowledgeGraph(uri="bolt://x", username="u", password="p", http_url="http://drevo:8080")


def _seed(name: str, score: float, project: str = "p") -> dict[str, Any]:
    return {"node": {"name": name, "project": project}, "score": score}


def _nb(name: str, relation: str, direction: str, project: str = "p") -> dict[str, Any]:
    return {
        "node": {"name": name, "project": project},
        "relation": relation,
        "direction": direction,
    }


# ── pure core: _expandable ────────────────────────────────────────────


def test_expandable_requires_name_and_project() -> None:
    assert _expandable({"name": "A", "project": "p"})
    assert not _expandable({"name": "A"})
    assert not _expandable({"project": "p"})
    assert not _expandable({"title": "doc"})


# ── graph layer: graph_search composition ─────────────────────────────


async def test_graph_search_expands_one_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = _kg()

    async def fake_hybrid(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"node": {"name": "A", "project": "p"}, "score": 0.1, "sources": {"fts": 1}}]

    async def fake_neighbors(name: str, project: str, limit: int) -> list[dict[str, Any]]:
        assert (name, project) == ("A", "p")
        return [_nb("B", "MENTIONS", "out"), _nb("C", "BLOCKS", "in")]

    monkeypatch.setattr(kg, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(kg, "neighbors", fake_neighbors)

    out = await kg.graph_search("q", "Entity", k=1, hops=1)
    assert [s["node"]["name"] for s in out["seeds"]] == ["A"]
    exp = {e["node"]["name"]: e for e in out["expanded"]}
    assert set(exp) == {"B", "C"}
    assert exp["B"]["distance"] == 1
    assert exp["B"]["via"] == {
        "from_name": "A",
        "from_project": "p",
        "relation": "MENTIONS",
        "direction": "out",
    }


async def test_graph_search_two_hops_walks_the_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = _kg()

    async def fake_hybrid(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [_seed("A", 0.1)]

    visited_frontier: list[str] = []

    async def fake_neighbors(name: str, project: str, limit: int) -> list[dict[str, Any]]:
        visited_frontier.append(name)
        graph = {
            "A": [_nb("B", "R", "out")],
            # B links onward to C, and back to seed A — the back-edge must not
            # re-add A (already visited).
            "B": [_nb("C", "R", "out"), _nb("A", "R", "in")],
        }
        return graph.get(name, [])

    monkeypatch.setattr(kg, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(kg, "neighbors", fake_neighbors)

    out = await kg.graph_search("q", "Entity", k=1, hops=2)
    dist = {e["node"]["name"]: e["distance"] for e in out["expanded"]}
    assert dist == {"B": 1, "C": 2}  # seed A never appears in expanded
    assert visited_frontier == ["A", "B"]  # BFS expanded A then B, then stopped


async def test_graph_search_skips_expansion_for_nodes_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = _kg()

    async def fake_hybrid(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"node": {"title": "doc", "labels": ["Doc"]}, "score": 0.1}]

    async def fake_neighbors(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise AssertionError("a node without (name, project) must not be expanded")

    monkeypatch.setattr(kg, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(kg, "neighbors", fake_neighbors)

    out = await kg.graph_search("q", "Doc", k=1, hops=1)
    assert out["expanded"] == []
    assert len(out["seeds"]) == 1


async def test_graph_search_dedups_neighbour_shared_by_two_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = _kg()

    async def fake_hybrid(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [_seed("A", 0.2), _seed("B", 0.1)]

    async def fake_neighbors(name: str, project: str, limit: int) -> list[dict[str, Any]]:
        return [_nb("S", "R", "out")]  # both A and B point at the same node S

    monkeypatch.setattr(kg, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(kg, "neighbors", fake_neighbors)

    out = await kg.graph_search("q", "Entity", k=2, hops=1)
    shared = [e for e in out["expanded"] if e["node"]["name"] == "S"]
    assert len(shared) == 1  # deduped
    assert shared[0]["via"]["from_name"] == "A"  # first frontier node wins the edge


async def test_graph_search_zero_hops_returns_only_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = _kg()

    async def fake_hybrid(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [_seed("A", 0.1)]

    async def fake_neighbors(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise AssertionError("hops=0 must not expand")

    monkeypatch.setattr(kg, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(kg, "neighbors", fake_neighbors)

    out = await kg.graph_search("q", "Entity", k=1, hops=0)
    assert out == {"seeds": [_seed("A", 0.1)], "expanded": []}


# ── server layer: the MCP tool ────────────────────────────────────────


def test_graph_search_tool_returns_seeds_and_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def graph_search(
            self,
            query: str,
            label: str,
            prop: str,
            k: int,
            hops: int,
            neighbors_per_node: int,
            candidates: int,
            rrf_k: int,
            model: str | None,
            fallback_to_fts: bool,
        ) -> dict[str, Any]:
            assert (
                query,
                label,
                prop,
                k,
                hops,
                neighbors_per_node,
                candidates,
                rrf_k,
                model,
                fallback_to_fts,
            ) == ("q", "Entity", "embedding", 5, 1, 10, 20, 60, None, True)
            return {"seeds": [{"node": {"name": "a"}, "score": 0.1}], "expanded": []}

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.graph_search("q", "Entity")))
    assert out == {"seeds": [{"node": {"name": "a"}, "score": 0.1}], "expanded": []}


def test_graph_search_tool_error_becomes_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def graph_search(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            raise ServiceUnavailable("down")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.graph_search("q", "Entity")))
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
    token = f"zqx{uuid.uuid4().hex[:10]}"  # distinctive so BM25 finds the seed
    seed_name = f"{fake.word()}-{token}"
    neighbour = f"{fake.word()}-{uuid.uuid4().hex[:8]}"

    kg = KnowledgeGraph(uri=url, username=_BOLT_USER, password=_BOLT_PASS)
    await kg.connect()
    try:
        await kg.create_entity(seed_name, "Note", project, [f"{fake.sentence()} {token}"])
        await kg.create_entity(neighbour, "Note", project, [fake.sentence()])
        await kg.create_relationship(seed_name, neighbour, "MENTIONS", project)

        # neighbors() returns the connected node with its relation + direction.
        neigh = await kg.neighbors(seed_name, project, 10)
        assert any(
            n["node"].get("name") == neighbour and n["relation"] == "MENTIONS" for n in neigh
        ), neigh

        # graph_search: BM25 finds the seed by its token, then one hop pulls in
        # the neighbour as context. (fallback_to_fts covers a no-embeddings drevo.)
        out = await kg.graph_search(token, "Note", k=5, hops=1)
        assert any(s["node"].get("name") == seed_name for s in out["seeds"]), out
        assert any(e["node"].get("name") == neighbour for e in out["expanded"]), out
    finally:
        await kg.run_cypher("MATCH (e:Entity {project: $p}) DETACH DELETE e", {"p": project})
        await kg.close()


@pytest.mark.skipif(not _RUN, reason="set DREVO_BOLT_URL to a reachable drevo Bolt server to run")
def test_graph_search_over_drevo_bolt() -> None:
    assert _BOLT_URL is not None  # narrowed by the skipif guard
    asyncio.run(_exercise(_BOLT_URL))
