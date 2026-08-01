"""Tests for `semantic_search` and its `embed_text` building block.

`semantic_search` closes the self-contained-RAG loop: it embeds a text query
via drevo's OpenAI-compatible `POST /v1/embeddings` (drevo issue #217) and then
vector-searches the resulting vector — text-in, ranked-nodes-out, one drevo
instance for graph + vectors + embeddings.

Two layers, mirroring the rest of the suite:

- **graph layer** — the real `KnowledgeGraph.embed_text` against a fake
  `httpx.AsyncClient` (no network): URL / payload shape, the `503`
  not-configured path, upstream errors, malformed and base64 responses, and the
  `embed → vector_search` composition.
- **server layer** — the `semantic_search` MCP tool with a stub `kg`, asserting
  the `[{"node", "score"}]` JSON shape and that a failure becomes a structured
  `embedding_error` envelope via `_guard`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from faker import Faker

from drevo_mcp_bolt import server
from drevo_mcp_bolt.graph import EmbeddingError, KnowledgeGraph


def _kg() -> KnowledgeGraph:
    return KnowledgeGraph(uri="bolt://x", username="u", password="p", http_url="http://drevo:8080")


def _install_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    payload: Any = None,
    text: str = "",
    raise_exc: Exception | None = None,
) -> dict[str, Any]:
    """Patch ``graph.httpx.AsyncClient`` with a fake that records the request
    and returns a preset response (or raises). Returns the capture dict."""
    captured: dict[str, Any] = {}

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code
            self.text = text

        def json(self) -> Any:
            return payload

    class _Client:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(self, url: str, json: Any = None) -> _Resp:
            captured["url"] = url
            captured["json"] = json
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr("drevo_mcp_bolt.graph.httpx.AsyncClient", _Client)
    return captured


# ── graph layer: embed_text ───────────────────────────────────────────


async def test_embed_text_posts_and_returns_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _install_httpx(monkeypatch, payload={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    text = Faker().sentence()
    vec = await _kg().embed_text(text)
    assert vec == [0.1, 0.2, 0.3]
    assert cap["url"] == "http://drevo:8080/v1/embeddings"
    assert cap["json"] == {"input": text}


async def test_embed_text_includes_model_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _install_httpx(monkeypatch, payload={"data": [{"embedding": [1.0]}]})
    await _kg().embed_text("x", model="text-embedding-3-small")
    assert cap["json"] == {"input": "x", "model": "text-embedding-3-small"}


async def test_embed_text_503_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx(monkeypatch, status_code=503, text="not configured")
    with pytest.raises(EmbeddingError) as ei:
        await _kg().embed_text("x")
    assert "not configured" in str(ei.value).lower()


async def test_embed_text_upstream_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx(monkeypatch, status_code=502, text="bad gateway")
    with pytest.raises(EmbeddingError):
        await _kg().embed_text("x")


async def test_embed_text_malformed_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx(monkeypatch, payload={"unexpected": True})
    with pytest.raises(EmbeddingError):
        await _kg().embed_text("x")


async def test_embed_text_rejects_base64_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx(monkeypatch, payload={"data": [{"embedding": "gAAAAA=="}]})
    with pytest.raises(EmbeddingError) as ei:
        await _kg().embed_text("x")
    assert "float vector" in str(ei.value)


async def test_embed_text_empty_text_rejected() -> None:
    with pytest.raises(EmbeddingError):
        await _kg().embed_text("")


async def test_embed_text_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx(monkeypatch, raise_exc=httpx.ConnectError("connection refused"))
    with pytest.raises(EmbeddingError):
        await _kg().embed_text("x")


# ── graph layer: semantic_search composition ──────────────────────────


async def test_semantic_search_embeds_then_vector_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = _kg()
    calls: dict[str, Any] = {}

    async def fake_embed(text: str, model: str | None = None) -> list[float]:
        calls["embed"] = (text, model)
        return [0.1, 0.2]

    async def fake_vs(label: str, prop: str, query: list[float], k: int) -> list[dict[str, Any]]:
        calls["vs"] = (label, prop, query, k)
        return [{"node": {"name": "a"}, "score": 0.9}]

    monkeypatch.setattr(kg, "embed_text", fake_embed)
    monkeypatch.setattr(kg, "vector_search", fake_vs)

    out = await kg.semantic_search("find me", "Entity", "embedding", 5)
    assert out == [{"node": {"name": "a"}, "score": 0.9}]
    assert calls["embed"] == ("find me", None)
    assert calls["vs"] == ("Entity", "embedding", [0.1, 0.2], 5)


async def test_semantic_search_falls_back_to_fts_when_embeddings_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Works without an LLM: a 503/upstream failure degrades to lexical BM25.
    kg = _kg()
    calls: dict[str, Any] = {}

    async def boom_embed(text: str, model: str | None = None) -> list[float]:
        raise EmbeddingError("drevo embeddings backend not configured (503)")

    async def fake_fts(query: str, k: int) -> list[dict[str, Any]]:
        calls["fts"] = (query, k)
        return [{"node": {"name": "lex"}, "score": 3.1}]

    monkeypatch.setattr(kg, "embed_text", boom_embed)
    monkeypatch.setattr(kg, "fts_search", fake_fts)

    out = await kg.semantic_search("find me", "Entity", "embedding", 7)
    assert out == [{"node": {"name": "lex"}, "score": 3.1}]
    assert calls["fts"] == ("find me", 7)  # query + k forwarded to FTS


async def test_semantic_search_reraises_when_fallback_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = _kg()

    async def boom_embed(text: str, model: str | None = None) -> list[float]:
        raise EmbeddingError("not configured")

    monkeypatch.setattr(kg, "embed_text", boom_embed)
    with pytest.raises(EmbeddingError):
        await kg.semantic_search("q", "Entity", fallback_to_fts=False)


# ── server layer: the MCP tool ────────────────────────────────────────


def test_semantic_search_tool_returns_scored_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def semantic_search(
            self,
            query: str,
            label: str,
            prop: str,
            k: int,
            model: str | None,
            fallback_to_fts: bool,
        ) -> list[dict[str, Any]]:
            assert (query, label, prop, k, model, fallback_to_fts) == (
                "q",
                "Entity",
                "embedding",
                10,
                None,
                True,
            )
            return [{"node": {"name": "a"}, "score": 0.9}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.semantic_search("q", "Entity")))
    assert out == [{"node": {"name": "a"}, "score": 0.9}]


def test_semantic_search_tool_error_becomes_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def semantic_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise EmbeddingError("drevo embeddings backend not configured (503)")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.semantic_search("q", "Entity")))
    assert out["ok"] is False
    assert out["error_type"] == "embedding_error"
