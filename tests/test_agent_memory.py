"""Tests for the agent-memory tools (drevo-mcp #15).

A drevo-native "context graph" for agent memory: ``add_message`` /
``get_conversation`` / ``recall_memory`` (short-term memory) and
``record_reasoning`` (reasoning memory). Two layers, mirroring the rest of the
suite:

- **server layer** — monkeypatch ``server.kg`` with a stub and assert each tool
  forwards its arguments, returns the documented JSON shape, and turns a failure
  into a structured error envelope via ``_guard``. No live server needed.
- **integration** — opt-in against a live drevo Bolt server (``DREVO_BOLT_URL``),
  exercising the real Cypher (``:NEXT`` chain, ``:INITIATED_BY``, recall with
  conversational context) end to end, then cleaning up the session subgraph.
  Skips unless the server is reachable, so offline CI stays green.
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


def test_add_message_forwards_and_returns_node(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def add_message(self, session: str, role: str, text: str) -> dict[str, Any]:
            assert (session, role, text) == ("s1", "user", "hello")
            return {"id": "abc", "seq": 1, "role": role, "text": text}

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.add_message("s1", "user", "hello")))
    assert out == {"id": "abc", "seq": 1, "role": "user", "text": "hello"}


def test_get_conversation_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    class _KG:
        async def get_conversation(self, session: str, limit: int) -> list[dict[str, Any]]:
            seen["limit"] = limit
            return [{"seq": 1, "role": "user", "text": "hi"}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.get_conversation("s1")))
    assert out == [{"seq": 1, "role": "user", "text": "hi"}]
    assert seen["limit"] == 50  # default surfaced to the graph layer


def test_recall_memory_defaults_and_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    class _KG:
        async def recall_memory(
            self, session: str, query: str, k: int, hops: int
        ) -> list[dict[str, Any]]:
            seen["k"], seen["hops"] = k, hops
            return [
                {
                    "message": {"seq": 3, "text": "about ports"},
                    "context": {"prev": None, "next": None},
                }
            ]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.recall_memory("s1", "ports")))
    assert out[0]["message"]["seq"] == 3
    assert seen == {"k": 5, "hops": 1}  # documented defaults


def test_record_reasoning_optional_args(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _KG:
        async def record_reasoning(
            self, session: str, step: str, tool: str | None, outcome: str | None
        ) -> dict[str, Any]:
            seen.update(session=session, step=step, tool=tool, outcome=outcome)
            return {"id": "t1", "step": step}

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.record_reasoning("s1", "chose port 7688")))
    assert out == {"id": "t1", "step": "chose port 7688"}
    assert seen == {"session": "s1", "step": "chose port 7688", "tool": None, "outcome": None}


def test_add_message_failure_becomes_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def add_message(self, session: str, role: str, text: str) -> dict[str, Any]:
            raise ServiceUnavailable("down")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.add_message("s1", "user", "hi")))
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
    session = f"it-mem-{uuid.uuid4().hex[:12]}"
    token = f"zqx{uuid.uuid4().hex[:10]}"

    kg = KnowledgeGraph(uri=url, username=_BOLT_USER, password=_BOLT_PASS)
    await kg.connect()
    try:
        first = await kg.add_message(session, "user", f"{fake.sentence()} about {token}")
        second = await kg.add_message(session, "assistant", f"noted {fake.sentence()}")
        # Sequences are assigned monotonically per session.
        assert first["seq"] == 1
        assert second["seq"] == 2

        # get_conversation replays in chronological order.
        convo = await kg.get_conversation(session)
        assert [m["seq"] for m in convo] == [1, 2]
        assert convo[0]["role"] == "user"

        # recall finds the message by its distinctive token and, with hops>=1,
        # carries the next turn as context.
        hits = await kg.recall_memory(session, token, k=5, hops=1)
        assert len(hits) == 1
        assert hits[0]["message"]["seq"] == 1
        assert hits[0]["context"]["next"]["seq"] == 2
        assert hits[0]["context"]["prev"] is None

        # hops=0 drops the context block.
        hits0 = await kg.recall_memory(session, token, k=5, hops=0)
        assert "context" not in hits0[0]

        # reasoning trace links to the newest message (seq 2).
        trace = await kg.record_reasoning(session, "decided X", tool="none", outcome="ok")
        assert trace["step"] == "decided X"
        linked = await kg.run_cypher(
            "MATCH (t:ReasoningTrace {session: $s})-[:INITIATED_BY]->(m:Message) "
            "RETURN m.seq AS seq",
            {"s": session},
        )
        assert linked and linked[0]["seq"] == 2
    finally:
        await kg.run_cypher("MATCH (n {session: $s}) DETACH DELETE n", {"s": session})
        await kg.close()


@pytest.mark.skipif(not _RUN, reason="set DREVO_BOLT_URL to a reachable drevo Bolt server to run")
def test_agent_memory_over_drevo_bolt() -> None:
    assert _BOLT_URL is not None  # narrowed by the skipif guard
    asyncio.run(_exercise(_BOLT_URL))
