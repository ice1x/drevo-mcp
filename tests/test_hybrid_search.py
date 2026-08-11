"""Tests for `hybrid_search` and its Reciprocal Rank Fusion (RRF) core.

`hybrid_search` runs one text query through *both* retrievers — lexical BM25
(`fts.search`) and semantic vectors (`embed_text` -> `drevo.vector.query`) — and
fuses the two rankings with RRF. Lexical catches exact terms/names/codes;
vectors catch meaning. RRF fuses on **rank**, not score, so the incomparable
BM25 and cosine scales never need calibrating.

Three layers, mirroring the rest of the suite:

- **pure core** — `_rrf_fuse` / `_node_key`: ranking, dedup across lists, the
  `rrf_k` flattening effect, and node identity. No I/O.
- **graph layer** — `KnowledgeGraph.hybrid_search` composition with the two
  retrievers monkeypatched: fusion happens, and the embeddings-unavailable path
  degrades to pure lexical BM25 (or re-raises when the fallback is disabled).
- **server layer** — the `hybrid_search` MCP tool with a stub `kg`, asserting
  the JSON row shape and that a failure becomes a structured error envelope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from drevo_mcp_bolt import server
from drevo_mcp_bolt.graph import EmbeddingError, KnowledgeGraph, _node_key, _rrf_fuse


def _kg() -> KnowledgeGraph:
    return KnowledgeGraph(uri="bolt://x", username="u", password="p", http_url="http://drevo:8080")


def _row(name: str, score: float, project: str = "p") -> dict[str, Any]:
    return {"node": {"name": name, "project": project}, "score": score}


# ── pure core: _node_key ──────────────────────────────────────────────


def test_node_key_prefers_name_and_project() -> None:
    # Same (name, project) is the same node regardless of other props/scores.
    assert _node_key({"name": "A", "project": "p", "x": 1}) == _node_key(
        {"name": "A", "project": "p", "x": 2}
    )
    # Different project is a different node.
    assert _node_key({"name": "A", "project": "p"}) != _node_key({"name": "A", "project": "q"})


def test_node_key_falls_back_to_content_when_no_name_project() -> None:
    # Nodes lacking name/project key on stable content, order-independent.
    k1 = _node_key({"title": "t", "v": 1})
    k2 = _node_key({"v": 1, "title": "t"})
    assert k1 == k2
    assert k1 != _node_key({"title": "t", "v": 2})


# ── pure core: _rrf_fuse ──────────────────────────────────────────────


def test_rrf_fuse_orders_by_reciprocal_rank() -> None:
    a, b, c = _row("A", 9.0), _row("B", 8.0), _row("C", 0.5)
    fused = _rrf_fuse({"fts": [a, b], "vector": [a, c]}, rrf_k=60)
    names = [r["node"]["name"] for r in fused]
    # A is rank-1 in both lists -> highest fused score; B, C each appear once.
    assert names[0] == "A"
    assert fused[0]["score"] == pytest.approx(1 / 61 + 1 / 61)
    others = {r["node"]["name"]: r["score"] for r in fused[1:]}
    assert others["B"] == pytest.approx(1 / 62)  # rank 2 in fts only
    assert others["C"] == pytest.approx(1 / 62)  # rank 2 in vector only


def test_rrf_fuse_dedups_and_records_source_ranks() -> None:
    a, b, c = _row("A", 9.0), _row("B", 8.0), _row("C", 0.5)
    fused = _rrf_fuse({"fts": [a, b], "vector": [a, c]}, rrf_k=60)
    by_name = {r["node"]["name"]: r for r in fused}
    # A merged into a single row carrying its rank in each source.
    assert len(fused) == 3
    assert by_name["A"]["sources"] == {"fts": 1, "vector": 1}
    # A node absent from a list records None for that source.
    assert by_name["B"]["sources"] == {"fts": 2, "vector": None}
    assert by_name["C"]["sources"] == {"fts": None, "vector": 2}


def test_rrf_fuse_larger_rrf_k_compresses_the_gap() -> None:
    # RRF's k damps the weight of top ranks: a bigger k flattens score gaps.
    lst = [_row("T", 1.0), _row("L", 1.0)]
    small = _rrf_fuse({"s": lst}, rrf_k=1)
    big = _rrf_fuse({"s": lst}, rrf_k=1000)
    gap_small = small[0]["score"] - small[1]["score"]
    gap_big = big[0]["score"] - big[1]["score"]
    assert gap_big < gap_small


def test_rrf_fuse_empty_input_is_empty() -> None:
    assert _rrf_fuse({"fts": [], "vector": []}, rrf_k=60) == []


# ── graph layer: hybrid_search composition ────────────────────────────


async def test_hybrid_search_fuses_fts_and_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = _kg()
    calls: dict[str, Any] = {}

    async def fake_fts(query: str, k: int) -> list[dict[str, Any]]:
        calls["fts"] = (query, k)
        return [_row("A", 5.0), _row("B", 4.0)]

    async def fake_embed(text: str, model: str | None = None) -> list[float]:
        calls["embed"] = (text, model)
        return [0.1, 0.2]

    async def fake_vs(label: str, prop: str, query: list[float], k: int) -> list[dict[str, Any]]:
        calls["vs"] = (label, prop, query, k)
        return [_row("A", 0.9), _row("C", 0.8)]

    monkeypatch.setattr(kg, "fts_search", fake_fts)
    monkeypatch.setattr(kg, "embed_text", fake_embed)
    monkeypatch.setattr(kg, "vector_search", fake_vs)

    out = await kg.hybrid_search("find me", "Entity", "embedding", k=2, candidates=20)

    # Both retrievers run over the same candidate pool; the query text is embedded.
    assert calls["fts"] == ("find me", 20)
    assert calls["embed"] == ("find me", None)
    assert calls["vs"] == ("Entity", "embedding", [0.1, 0.2], 20)
    # A tops the ranking (rank-1 in both) and only k rows are returned.
    assert len(out) == 2
    assert out[0]["node"]["name"] == "A"
    assert out[0]["sources"] == {"fts": 1, "vector": 1}


async def test_hybrid_search_candidate_pool_never_below_k(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = _kg()
    seen: dict[str, int] = {}

    async def fake_fts(query: str, k: int) -> list[dict[str, Any]]:
        seen["fts_k"] = k
        return []

    async def fake_embed(text: str, model: str | None = None) -> list[float]:
        return [0.1]

    async def fake_vs(label: str, prop: str, query: list[float], k: int) -> list[dict[str, Any]]:
        seen["vs_k"] = k
        return []

    monkeypatch.setattr(kg, "fts_search", fake_fts)
    monkeypatch.setattr(kg, "embed_text", fake_embed)
    monkeypatch.setattr(kg, "vector_search", fake_vs)

    await kg.hybrid_search("q", "Entity", k=10, candidates=3)
    # candidates < k would silently truncate the pool below k — clamp up.
    assert seen["fts_k"] == 10
    assert seen["vs_k"] == 10


async def test_hybrid_search_degrades_to_fts_on_embedding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Works without an LLM: a 503/upstream failure degrades to lexical BM25 rows,
    # unfused (no RRF `sources`), exactly like semantic_search's fallback.
    kg = _kg()
    calls: dict[str, Any] = {}

    async def fake_fts(query: str, k: int) -> list[dict[str, Any]]:
        calls["fts"] = (query, k)
        return [_row("A", 5.0), _row("B", 4.0), _row("C", 3.0)]

    async def boom_embed(text: str, model: str | None = None) -> list[float]:
        raise EmbeddingError("drevo embeddings backend not configured (503)")

    def _no_vector(*_a: Any, **_k: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("vector_search must not run when embedding fails")

    monkeypatch.setattr(kg, "fts_search", fake_fts)
    monkeypatch.setattr(kg, "embed_text", boom_embed)
    monkeypatch.setattr(kg, "vector_search", _no_vector)

    out = await kg.hybrid_search("find me", "Entity", k=2, candidates=20)
    assert out == [_row("A", 5.0), _row("B", 4.0)]  # top-k BM25, no "sources" key
    assert "sources" not in out[0]


async def test_hybrid_search_reraises_when_fallback_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = _kg()

    async def fake_fts(query: str, k: int) -> list[dict[str, Any]]:
        return [_row("A", 5.0)]

    async def boom_embed(text: str, model: str | None = None) -> list[float]:
        raise EmbeddingError("not configured")

    monkeypatch.setattr(kg, "fts_search", fake_fts)
    monkeypatch.setattr(kg, "embed_text", boom_embed)
    with pytest.raises(EmbeddingError):
        await kg.hybrid_search("q", "Entity", fallback_to_fts=False)


# ── server layer: the MCP tool ────────────────────────────────────────


def test_hybrid_search_tool_returns_fused_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def hybrid_search(
            self,
            query: str,
            label: str,
            prop: str,
            k: int,
            candidates: int,
            rrf_k: int,
            model: str | None,
            fallback_to_fts: bool,
        ) -> list[dict[str, Any]]:
            assert (query, label, prop, k, candidates, rrf_k, model, fallback_to_fts) == (
                "q",
                "Entity",
                "embedding",
                10,
                20,
                60,
                None,
                True,
            )
            return [{"node": {"name": "a"}, "score": 0.03, "sources": {"fts": 1, "vector": 2}}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.hybrid_search("q", "Entity")))
    assert out == [{"node": {"name": "a"}, "score": 0.03, "sources": {"fts": 1, "vector": 2}}]


def test_hybrid_search_tool_error_becomes_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def hybrid_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise EmbeddingError("drevo embeddings backend not configured (503)")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.hybrid_search("q", "Entity", fallback_to_fts=False)))
    assert out["ok"] is False
    assert out["error_type"] == "embedding_error"
