"""Offline tests for the result/error semantics (no live Bolt server).

Two things this locks down, both previously broken:

1. **Graph layer** — mutations that target a missing entity used to return an
   empty ``{}`` and silently no-op. They must now raise a typed
   ``KnowledgeGraphError`` so the caller can tell "nothing happened" from
   "it worked".
2. **Server layer** — a stopped container (or a bad query) used to bubble a raw
   driver exception up to FastMCP, surfacing as an ``isError`` tool result with
   an opaque stack-trace-y message. The tools must instead return a structured
   JSON envelope ``{"ok": false, "error": ..., "error_type": ...}``.

Both are exercised without a database: the graph tests inject a tiny fake Bolt
driver, and the server tests monkeypatch ``server.kg`` with a stub that raises.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable

import drevo_mcp_bolt.graph as graph
import drevo_mcp_bolt.server as server
from drevo_mcp_bolt.graph import (
    EntityNotFoundError,
    KnowledgeGraph,
    RelationshipEndpointsNotFoundError,
)

# ── Fake Bolt driver ──────────────────────────────────────────────────
# A minimal async stand-in: every ``session().run()`` yields a result whose
# ``single()`` returns the one canned record (or ``None`` to model "no match").


class _FakeResult:
    def __init__(self, record: Any) -> None:
        self._record = record

    async def single(self) -> Any:
        return self._record


class _FakeSession:
    def __init__(self, record: Any) -> None:
        self._record = record

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def run(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult(self._record)


class _FakeDriver:
    def __init__(self, record: Any) -> None:
        self._record = record

    def session(self, **_kwargs: Any) -> _FakeSession:
        return _FakeSession(self._record)

    async def close(self) -> None:
        return None


def _kg_returning(record: Any) -> KnowledgeGraph:
    kg = KnowledgeGraph(uri="bolt://x", username="u", password="p")
    kg._driver = _FakeDriver(record)  # type: ignore[assignment]
    return kg


# ── Graph layer: missing targets raise instead of silently no-op ──────


def test_add_observations_missing_entity_raises() -> None:
    kg = _kg_returning(None)  # MATCH found nothing
    with pytest.raises(EntityNotFoundError) as excinfo:
        asyncio.run(kg.add_observations("Ghost", "proj", ["note"]))
    assert "Ghost" in str(excinfo.value)
    assert "proj" in str(excinfo.value)


def test_create_relationship_missing_endpoint_raises() -> None:
    kg = _kg_returning(None)  # one or both entities absent
    with pytest.raises(RelationshipEndpointsNotFoundError) as excinfo:
        asyncio.run(kg.create_relationship("A", "B", "KNOWS", "proj"))
    assert "A" in str(excinfo.value) and "B" in str(excinfo.value)


def test_delete_entity_reports_false_without_raising() -> None:
    # Delete is not an error when there's nothing to delete — it just reports it.
    kg = _kg_returning({"deleted": 0})
    assert asyncio.run(kg.delete_entity("Ghost", "proj")) is False


# ── Graph layer: connect() is best-effort ─────────────────────────────


def test_connect_swallows_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down container must not stop the server from starting.

    ``connect()`` should build the driver and swallow a connectivity failure so
    the MCP still launches; tools then surface a structured error per call.
    """

    class _Unreachable:
        async def verify_connectivity(self) -> None:
            raise ServiceUnavailable("container is down")

        def session(self, **_kwargs: Any) -> Any:
            raise AssertionError("indexes must not be attempted when unreachable")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(graph.AsyncGraphDatabase, "driver", lambda *a, **k: _Unreachable())
    kg = KnowledgeGraph(uri="bolt://x", username="u", password="p")
    asyncio.run(kg.connect())  # must NOT raise
    assert kg._driver is not None


# ── Server layer: structured error envelope ───────────────────────────


class _RaisingKG:
    """Stub knowledge graph whose every method raises a preset exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __getattr__(self, _name: str) -> Any:
        async def _raise(*_a: Any, **_k: Any) -> Any:
            raise self._exc

        return _raise


@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        (EntityNotFoundError("Ghost", "proj"), "not_found"),
        (ServiceUnavailable("container is down"), "unavailable"),
        (ClientError("Invalid syntax"), "query_error"),
        (RuntimeError("KnowledgeGraph is not connected; call connect() first"), "not_connected"),
    ],
)
def test_add_observations_wraps_errors(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected_type: str
) -> None:
    monkeypatch.setattr(server, "kg", _RaisingKG(exc))
    out = json.loads(asyncio.run(server.add_observations("E", "proj", ["x"])))
    assert out["ok"] is False
    assert out["error_type"] == expected_type
    assert out["error"]  # non-empty human-readable message


def test_container_down_yields_structured_error_not_iserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The headline fix: a stopped container returns a clean JSON envelope, not a
    # raw driver exception (which FastMCP would surface as an isError result).
    monkeypatch.setattr(server, "kg", _RaisingKG(ServiceUnavailable("down")))
    out = json.loads(asyncio.run(server.create_entity("E", "Person", "proj")))
    assert out == {
        "ok": False,
        "error_type": "unavailable",
        "error": out["error"],
    }
    assert "down" in out["error"]


def test_run_cypher_bad_query_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "kg", _RaisingKG(ClientError("Invalid input")))
    out = json.loads(asyncio.run(server.run_cypher("NOT CYPHER")))
    assert out["ok"] is False and out["error_type"] == "query_error"


def test_successful_call_shape_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard must be transparent on success: it returns whatever the tool built.
    class _OkKG:
        async def list_projects(self) -> list[str]:
            return ["alpha", "beta"]

    monkeypatch.setattr(server, "kg", _OkKG())
    out = json.loads(asyncio.run(server.list_projects()))
    assert out == ["alpha", "beta"]
