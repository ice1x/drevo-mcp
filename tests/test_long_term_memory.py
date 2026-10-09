"""Tests for the long-term memory tools (context graph, drevo#533).

``remember_entity`` / ``assert_fact`` / ``retract_fact`` / ``facts_at`` are thin
wrappers over drevo's native ``drevo.memory.*`` procedures: POLE+O entities and
facts with a validity window (``valid_from`` / ``valid_until``), superseded
facts kept as history. No memory logic is duplicated here — the MCP only
forwards the call — so any Bolt client gets the same behaviour from drevo.

- **graph layer** — a capturing fake driver asserts each method issues the
  native ``CALL drevo.memory.*`` with the right parameters and shapes the rows.
- **server layer** — monkeypatched ``server.kg``: arguments forwarded, defaults
  surfaced, JSON returned.
- **integration** — opt-in against a live drevo (``DREVO_BOLT_URL``); skips when
  the server is unreachable or predates the procedures.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import pytest
from faker import Faker
from neo4j.exceptions import ClientError, ServiceUnavailable

from drevo_mcp_bolt import server
from drevo_mcp_bolt.graph import KnowledgeGraph

# ── Graph layer (capturing fake driver) ───────────────────────────────


class _Result:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None

    def __aiter__(self) -> "_Result":
        self._it = iter(self._records)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Session:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], records: list[dict[str, Any]]):
        self._calls = calls
        self._records = records

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def run(self, query: str, parameters: dict[str, Any] | None = None, **kw: Any) -> _Result:
        self._calls.append((query, {**(parameters or {}), **kw}))
        return _Result(self._records)


class _Driver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._records = records

    def session(self, **_kw: Any) -> _Session:
        return _Session(self.calls, self._records)

    async def close(self) -> None:
        return None


def _kg(records: list[dict[str, Any]]) -> tuple[KnowledgeGraph, _Driver]:
    kg = KnowledgeGraph(uri="bolt://x", username="u", password="p")
    drv = _Driver(records)
    kg._driver = drv  # type: ignore[assignment]
    return kg, drv


def test_remember_entity_calls_the_native_procedure() -> None:
    kg, drv = _kg([{"entity": {"name": "Dana", "type": "PERSON", "embedding": [0.1] * 1536}}])
    out = asyncio.run(kg.remember_entity("s1", "Dana", "person", "engineer"))
    query, params = drv.calls[0]
    assert "CALL drevo.memory.rememberEntity($session, $name, $type, $description)" in query
    assert params == {
        "session": "s1",
        "name": "Dana",
        "type": "person",
        "description": "engineer",
    }
    # Vectors are stripped from what the model sees.
    assert out == {"name": "Dana", "type": "PERSON"}


def test_assert_fact_forwards_exclusive_and_shapes_the_fact() -> None:
    row = {
        "id": "f1",
        "relation": "WORKS_AT",
        "valid_from": "2026-09-29T10:00:00.000Z",
        "valid_until": None,
    }
    kg, drv = _kg([row])
    out = asyncio.run(kg.assert_fact("Dana", "WORKS_AT", "Acme", exclusive=True))
    query, params = drv.calls[0]
    assert "CALL drevo.memory.assertFact($subject, $relation, $object, $exclusive)" in query
    assert params == {
        "subject": "Dana",
        "relation": "WORKS_AT",
        "object": "Acme",
        "exclusive": True,
    }
    assert out == {"subject": "Dana", "object": "Acme", **row}


def test_retract_fact_returns_every_closed_fact() -> None:
    rows = [{"id": "f1", "relation": "WORKS_AT", "valid_from": "a", "valid_until": "b"}]
    kg, drv = _kg(rows)
    out = asyncio.run(kg.retract_fact("Dana", "WORKS_AT", "Acme"))
    query, params = drv.calls[0]
    assert "CALL drevo.memory.retractFact($subject, $relation, $object)" in query
    assert params == {"subject": "Dana", "relation": "WORKS_AT", "object": "Acme"}
    assert out == [{"subject": "Dana", "object": "Acme", **rows[0]}]


def test_facts_at_passes_as_of_and_returns_rows() -> None:
    rows = [
        {
            "subject": "Dana",
            "relation": "WORKS_AT",
            "object": "Globex",
            "valid_from": "2026-09-29T11:00:00.000Z",
            "valid_until": None,
        }
    ]
    kg, drv = _kg(rows)
    assert asyncio.run(kg.facts_at("Dana")) == rows
    query, params = drv.calls[0]
    assert "CALL drevo.memory.factsAt($name, $as_of)" in query
    assert params == {"name": "Dana", "as_of": None}  # null = now

    asyncio.run(kg.facts_at("Dana", "2026-01-01T00:00:00.000Z"))
    assert drv.calls[1][1]["as_of"] == "2026-01-01T00:00:00.000Z"


# ── Server layer (mocked kg) ──────────────────────────────────────────


def test_remember_entity_tool_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _KG:
        async def remember_entity(
            self, session: str | None, name: str, entity_type: str, description: str | None
        ) -> dict[str, Any]:
            seen.update(session=session, name=name, type=entity_type, description=description)
            return {"name": name, "type": entity_type.upper()}

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.remember_entity("Paris", "location")))
    assert out == {"name": "Paris", "type": "LOCATION"}
    assert seen == {"session": None, "name": "Paris", "type": "location", "description": None}


def test_assert_fact_tool_defaults_to_non_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _KG:
        async def assert_fact(
            self, subject: str, relation: str, obj: str, exclusive: bool
        ) -> dict[str, Any]:
            seen["exclusive"] = exclusive
            return {"subject": subject, "relation": relation, "object": obj}

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.assert_fact("Dana", "KNOWS", "Bob")))
    assert out == {"subject": "Dana", "relation": "KNOWS", "object": "Bob"}
    assert seen["exclusive"] is False


def test_retract_and_facts_at_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def retract_fact(self, subject: str, relation: str, obj: str) -> list[dict[str, Any]]:
            return [{"subject": subject, "relation": relation, "object": obj}]

        async def facts_at(self, name: str, as_of: str | None) -> list[dict[str, Any]]:
            return [{"subject": name, "as_of": as_of}]

    monkeypatch.setattr(server, "kg", _KG())
    assert json.loads(asyncio.run(server.retract_fact("Dana", "WORKS_AT", "Acme"))) == [
        {"subject": "Dana", "relation": "WORKS_AT", "object": "Acme"}
    ]
    assert json.loads(asyncio.run(server.facts_at("Dana"))) == [{"subject": "Dana", "as_of": None}]


def test_tool_failure_becomes_an_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    class _KG:
        async def assert_fact(self, *a: Any) -> dict[str, Any]:
            raise ClientError("no memory entity named `Ghost`")

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.assert_fact("Ghost", "KNOWS", "Bob")))
    assert "error" in out


# ── Integration (opt-in, live drevo) ──────────────────────────────────

_BOLT_URL = os.environ.get("DREVO_BOLT_URL")


@pytest.mark.skipif(not _BOLT_URL, reason="set DREVO_BOLT_URL to run against a live drevo")
def test_long_term_memory_round_trip_live() -> None:
    fake = Faker()
    Faker.seed(533)
    tag = uuid.uuid4().hex[:8]
    person = f"{fake.first_name()}-{tag}"
    first, second = f"{fake.company()}-{tag}", f"{fake.company()}-{tag}"

    async def scenario() -> None:
        kg = KnowledgeGraph(
            uri=_BOLT_URL or "",
            username=os.environ.get("DREVO_BOLT_USER", "neo4j"),
            password=os.environ.get("DREVO_BOLT_PASS", "drevo"),
        )
        await kg.connect()
        try:
            try:
                await kg.remember_entity(None, person, "PERSON", None)
            except ServiceUnavailable:
                pytest.skip("drevo Bolt server unreachable")
            except ClientError as exc:
                if "no such procedure" in str(exc):
                    pytest.skip("live drevo predates drevo.memory.* (#533)")
                raise
            await kg.remember_entity(None, first, "ORGANIZATION", None)
            await kg.remember_entity(None, second, "ORGANIZATION", None)
            await kg.assert_fact(person, "WORKS_AT", first, exclusive=True)
            await asyncio.sleep(0.01)
            await kg.assert_fact(person, "WORKS_AT", second, exclusive=True)
            now = await kg.facts_at(person)
            assert [(f["relation"], f["object"]) for f in now] == [("WORKS_AT", second)]
        finally:
            await kg.run_cypher(
                "MATCH (e:Entity) WHERE e.name IN $names DETACH DELETE e",
                {"names": [person, first, second]},
            )
            await kg.close()

    asyncio.run(scenario())
