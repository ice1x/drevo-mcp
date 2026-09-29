"""Tests for the ``stable_matching`` tool (drevo#541).

A thin wrapper over drevo's native ``drevo.stableMatching`` procedure
(Gale–Shapley): the algorithm lives in drevo, the MCP only forwards the call and
shapes the rows.

- **graph layer** — a capturing fake driver asserts the exact ``CALL`` and
  parameters, and that node vectors are stripped from the matched pairs.
- **server layer** — monkeypatched ``server.kg``: defaults and JSON output.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from drevo_mcp_bolt import server
from drevo_mcp_bolt.graph import KnowledgeGraph


class _Result:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._it = iter(records)

    def __aiter__(self) -> "_Result":
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Session:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], records: list[dict[str, Any]]):
        self._calls, self._records = calls, records

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


def test_stable_matching_calls_the_native_procedure() -> None:
    rows = [
        {
            "proposer": {"name": "ann", "embedding": [0.5] * 1536},
            "acceptor": {"name": "yan"},
            "proposer_rank": 2,
            "acceptor_rank": 1,
        }
    ]
    kg = KnowledgeGraph(uri="bolt://x", username="u", password="p")
    drv = _Driver(rows)
    kg._driver = drv  # type: ignore[assignment]

    out = asyncio.run(kg.stable_matching("Mentee", "Mentor", "PREFERS", "rank"))

    query, params = drv.calls[0]
    assert (
        "CALL drevo.stableMatching($proposer_label, $acceptor_label, $rel_type, $rank_property)"
        in query
    )
    assert params == {
        "proposer_label": "Mentee",
        "acceptor_label": "Mentor",
        "rel_type": "PREFERS",
        "rank_property": "rank",
    }
    assert out == [
        {
            "proposer": {"name": "ann"},  # embedding stripped
            "acceptor": {"name": "yan"},
            "proposer_rank": 2,
            "acceptor_rank": 1,
        }
    ]


def test_stable_matching_tool_defaults_rank_property(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    class _KG:
        async def stable_matching(
            self, proposer_label: str, acceptor_label: str, rel_type: str, rank_property: str
        ) -> list[dict[str, Any]]:
            seen.update(p=proposer_label, a=acceptor_label, r=rel_type, k=rank_property)
            return [{"proposer": {"name": "ann"}, "acceptor": {"name": "yan"}}]

    monkeypatch.setattr(server, "kg", _KG())
    out = json.loads(asyncio.run(server.stable_matching("Mentee", "Mentor", "PREFERS")))
    assert out == [{"proposer": {"name": "ann"}, "acceptor": {"name": "yan"}}]
    assert seen == {"p": "Mentee", "a": "Mentor", "r": "PREFERS", "k": "rank"}
