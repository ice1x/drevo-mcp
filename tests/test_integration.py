"""End-to-end: the write/query/migration tools against a live drevo Bolt server.

Unlike the monorepo version (which built and spawned a ``drevo-server`` binary),
this standalone repo drives whatever Bolt server ``DREVO_BOLT_URL`` points at —
typically the published ``ice1x/drevo`` container started with Bolt enabled
(``docker compose up -d`` publishes 7687). The test is **opt-in**: it skips
unless ``DREVO_BOLT_URL`` is set *and* the port is actually open, so offline CI
stays green.

Synchronous shell (``asyncio.run``) around the async ``KnowledgeGraph`` the MCP
tools use, exercising the full mutate→read→migrate→cypher round trip and
cleaning up the data it writes.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from faker import Faker

from drevo_mcp_bolt.graph import KnowledgeGraph

_BOLT_URL = os.environ.get("DREVO_BOLT_URL")
_BOLT_USER = os.environ.get("DREVO_BOLT_USER", "neo4j")
_BOLT_PASS = os.environ.get("DREVO_BOLT_PASSWORD", "drevo")


def _bolt_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 7687
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_RUN = _BOLT_URL is not None and _bolt_reachable(_BOLT_URL)


async def _exercise(url: str) -> None:
    fake = Faker()
    # A unique project namespace so a real graph is never polluted and parallel
    # runs never collide; everything created here is torn down in `finally`.
    project = f"it-{uuid.uuid4().hex[:12]}"
    alice, bob = fake.unique.first_name(), fake.unique.first_name()
    observation = fake.sentence()

    kg = KnowledgeGraph(uri=url, username=_BOLT_USER, password=_BOLT_PASS)
    await kg.connect()
    try:
        # ── create / update entity ───────────────────────────────────────
        ent = await kg.create_entity(alice, "Person", project, [observation], {"team": "core"})
        assert ent["name"] == alice
        assert "Entity" in ent["labels"]
        assert ent["team"] == "core"  # SET += merged the extra property

        await kg.create_entity(bob, "Person", project)
        more = fake.sentence()
        updated = await kg.add_observations(alice, project, [more])
        assert more in updated["observations"]

        # ── relationship ─────────────────────────────────────────────────
        rel = await kg.create_relationship(alice, bob, "KNOWS", project)
        assert rel["type"] == "KNOWS"
        assert rel["from"] == alice and rel["to"] == bob

        # ── queries ──────────────────────────────────────────────────────
        got = await kg.get_entity(alice, project)
        assert got["entity"]["name"] == alice
        assert any(r["target"] == bob for r in got["outgoing_relations"])

        found = await kg.search(alice, project)
        assert any(e["name"] == alice for e in found)

        graph = await kg.get_project_graph(project)
        assert {e["name"] for e in graph["entities"]} >= {alice, bob}

        assert project in await kg.list_projects()

        # ── migrations ───────────────────────────────────────────────────
        mig = await kg.add_migration(project, "tag everyone", "MATCH (e:Entity) RETURN count(e)")
        assert mig["seq"] == 1 and mig["applied"] is False
        applied = await kg.apply_migration(project, 1)
        assert applied["applied"] is True
        history = await kg.get_migrations(project)
        assert len(history) == 1

        # ── raw cypher ───────────────────────────────────────────────────
        rows = await kg.run_cypher(
            "MATCH (e:Entity {project: $p}) RETURN count(e) AS n", {"p": project}
        )
        assert rows[0]["n"] == 2

        # ── teardown ─────────────────────────────────────────────────────
        assert await kg.delete_relationship(alice, bob, "KNOWS", project) is True
        assert await kg.delete_entity(alice, project) is True
        assert await kg.delete_entity(bob, project) is True
        await kg.run_cypher("MATCH (m:Migration {project: $p}) DELETE m", {"p": project})
    finally:
        await kg.close()


@pytest.mark.skipif(not _RUN, reason="set DREVO_BOLT_URL to a reachable drevo Bolt server to run")
def test_dropin_crud_over_drevo_bolt() -> None:
    assert _BOLT_URL is not None  # narrowed by the skipif guard above
    asyncio.run(_exercise(_BOLT_URL))
