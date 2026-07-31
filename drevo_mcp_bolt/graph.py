"""Knowledge-graph operations over a Bolt connection.

Adapted verbatim from the Neo4j knowledge-graph MCP — every Cypher query is
unchanged — and pointed at drevo's Neo4j-compatible Bolt endpoint. drevo runs
the same queries (``MERGE`` / ``datetime()`` / ``SET +=`` / map projection /
``labels()`` / ``type()`` / ``properties()`` / ``OPTIONAL MATCH`` / ``collect``)
so this is a true drop-in. The one drevo difference: ``CREATE INDEX`` schema DDL
is not supported (drevo auto-indexes), so ``_ensure_indexes`` is best-effort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired


class KnowledgeGraphError(Exception):
    """Base for knowledge-graph domain errors (as opposed to driver errors)."""


class EntityNotFoundError(KnowledgeGraphError):
    """A mutation targeted an entity that does not exist."""

    def __init__(self, name: str, project: str) -> None:
        self.name = name
        self.project = project
        super().__init__(f"entity {name!r} not found in project {project!r}")


class RelationshipEndpointsNotFoundError(KnowledgeGraphError):
    """A relationship could not be created because an endpoint is missing."""

    def __init__(self, from_entity: str, to_entity: str, project: str) -> None:
        self.from_entity = from_entity
        self.to_entity = to_entity
        self.project = project
        super().__init__(
            "cannot create relationship: one or both entities missing in "
            f"project {project!r} (from={from_entity!r}, to={to_entity!r})"
        )


class MigrationNotFoundError(KnowledgeGraphError):
    """A migration to apply does not exist, or was already applied."""

    def __init__(self, project: str, seq: int) -> None:
        self.project = project
        self.seq = seq
        super().__init__(f"migration seq={seq} not found or already applied in project {project!r}")


class EmbeddingError(KnowledgeGraphError):
    """Turning text into an embedding failed.

    Raised by :meth:`KnowledgeGraph.embed_text` when drevo's OpenAI-compatible
    ``POST /v1/embeddings`` (drevo issue #217) is unreachable, answers a non-2xx
    status (notably ``503`` when the embeddings backend is not configured), or
    returns a body this client cannot turn into a float vector.
    """


@dataclass
class KnowledgeGraph:
    """Manages knowledge-graph operations against a Bolt server (drevo)."""

    uri: str
    username: str
    password: str
    database: str = "neo4j"
    # drevo's HTTP API base (the OpenAI-compatible embeddings endpoint lives at
    # ``{http_url}/v1/embeddings``). Separate from the Bolt ``uri`` because
    # embedding generation is HTTP-only in drevo (issue #217).
    http_url: str = "http://localhost:8080"
    http_timeout: float = 30.0
    _driver: AsyncDriver | None = field(default=None, repr=False)

    @property
    def _drv(self) -> AsyncDriver:
        if self._driver is None:
            raise RuntimeError("KnowledgeGraph is not connected; call connect() first")
        return self._driver

    async def connect(self) -> None:
        # Note: no `notifications_disabled_categories` — that is a Bolt-5.x
        # feature, and drevo negotiates Bolt 4.4. Leaving it off keeps the
        # client compatible with both drevo and Neo4j.
        self._driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.username, self.password),
        )
        # Best-effort: if the drevo container is down at launch, still start the
        # server so it can come up and report a structured per-call error, rather
        # than crashing the MCP process (which the client shows as "failed to
        # start"). The driver is kept; each tool retries the connection and the
        # server layer turns the failure into an "unavailable" envelope.
        try:
            await self._driver.verify_connectivity()
        except (ServiceUnavailable, SessionExpired, OSError):
            return
        await self._ensure_indexes()

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()

    async def _ensure_indexes(self) -> None:
        """Best-effort schema indexes.

        Neo4j needs explicit ``CREATE INDEX``; drevo auto-indexes node titles /
        kinds / properties and does **not** support schema DDL. So each index
        statement is attempted and any failure is ignored — on drevo this is a
        no-op, on Neo4j it creates the indexes as before.
        """
        statements = (
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX entity_project IF NOT EXISTS FOR (e:Entity) ON (e.project)",
            "CREATE INDEX migration_project IF NOT EXISTS FOR (m:Migration) ON (m.project)",
        )
        async with self._drv.session(database=self.database) as session:
            for stmt in statements:
                try:
                    await session.run(stmt)
                except Exception:  # noqa: BLE001 — drevo has no schema DDL; ignore.
                    pass

    # ── Entities ──────────────────────────────────────────────────────

    async def create_entity(
        self,
        name: str,
        entity_type: str,
        project: str,
        observations: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or merge an entity node in the knowledge graph.

        drevo's full-text index (``CALL fts.search``) indexes a node's
        ``title`` + ``body``, **not** its arbitrary properties — so the
        ``name`` / ``observations`` we store would be invisible to search. We
        mirror them into ``body`` on every write to make entities discoverable
        via ``fts_search``. ``title`` is deliberately left unset: drevo enforces
        title uniqueness, which ``name`` (unique only *per project*) would
        violate.
        """
        props = properties or {}
        obs = observations or []
        query = """
        MERGE (e:Entity {name: $name, project: $project})
        ON CREATE SET
            e.type = $entity_type,
            e.observations = $observations,
            e.created_at = datetime(),
            e.updated_at = datetime()
        ON MATCH SET
            e.type = $entity_type,
            e.observations = e.observations + $observations,
            e.updated_at = datetime()
        SET e += $properties
        SET e.body = e.name + ' ' + reduce(acc = '', o IN coalesce(e.observations, []) | acc + ' ' + o)
        RETURN e{.*, labels: labels(e)} AS entity
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(
                query,
                name=name,
                project=project,
                entity_type=entity_type,
                observations=obs,
                properties=props,
            )
            record = await result.single()
            return dict(record["entity"]) if record else {}

    async def add_observations(
        self, name: str, project: str, observations: list[str]
    ) -> dict[str, Any]:
        """Append observations to an existing entity.

        Also refreshes the ``body`` mirror so the new observations become
        full-text searchable (see :meth:`create_entity`).
        """
        query = """
        MATCH (e:Entity {name: $name, project: $project})
        SET e.observations = e.observations + $observations,
            e.updated_at = datetime()
        SET e.body = e.name + ' ' + reduce(acc = '', o IN coalesce(e.observations, []) | acc + ' ' + o)
        RETURN e{.*, labels: labels(e)} AS entity
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, name=name, project=project, observations=observations)
            record = await result.single()
            if not record:
                raise EntityNotFoundError(name, project)
            return dict(record["entity"])

    async def delete_entity(self, name: str, project: str) -> bool:
        """Delete an entity and all its relationships."""
        query = """
        MATCH (e:Entity {name: $name, project: $project})
        DETACH DELETE e
        RETURN count(e) AS deleted
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, name=name, project=project)
            record = await result.single()
            return bool(record and record["deleted"] > 0)

    # ── Relationships ─────────────────────────────────────────────────

    async def create_relationship(
        self,
        from_entity: str,
        to_entity: str,
        relation_type: str,
        project: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a typed relationship between two entities."""
        props = properties or {}
        # Cypher doesn't support parameterised relationship types, so we
        # sanitise and interpolate the type name.
        safe_type = "".join(c if c.isalnum() or c == "_" else "_" for c in relation_type.upper())
        query = f"""
        MATCH (a:Entity {{name: $from_name, project: $project}})
        MATCH (b:Entity {{name: $to_name, project: $project}})
        MERGE (a)-[r:{safe_type}]->(b)
        SET r += $properties, r.created_at = coalesce(r.created_at, datetime())
        RETURN type(r) AS type,
               a.name AS from, b.name AS to,
               properties(r) AS properties
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(
                query,
                from_name=from_entity,
                to_name=to_entity,
                project=project,
                properties=props,
            )
            record = await result.single()
            if not record:
                raise RelationshipEndpointsNotFoundError(from_entity, to_entity, project)
            return dict(record)

    async def delete_relationship(
        self, from_entity: str, to_entity: str, relation_type: str, project: str
    ) -> bool:
        """Delete a specific relationship between two entities."""
        safe_type = "".join(c if c.isalnum() or c == "_" else "_" for c in relation_type.upper())
        query = f"""
        MATCH (a:Entity {{name: $from_name, project: $project}})
              -[r:{safe_type}]->
              (b:Entity {{name: $to_name, project: $project}})
        DELETE r
        RETURN count(r) AS deleted
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(
                query, from_name=from_entity, to_name=to_entity, project=project
            )
            record = await result.single()
            return bool(record and record["deleted"] > 0)

    # ── Queries ───────────────────────────────────────────────────────

    async def get_entity(self, name: str, project: str) -> dict[str, Any]:
        """Get an entity with all its relationships (context for an LLM)."""
        query = """
        MATCH (e:Entity {name: $name, project: $project})
        OPTIONAL MATCH (e)-[r]->(target:Entity)
        OPTIONAL MATCH (source:Entity)-[ri]->(e)
        WITH e,
             collect(DISTINCT {type: type(r), target: target.name, target_type: target.type}) AS outgoing,
             collect(DISTINCT {type: type(ri), source: source.name, source_type: source.type}) AS incoming
        RETURN e{.*, labels: labels(e)} AS entity,
               [x IN outgoing WHERE x.target IS NOT NULL] AS outgoing_relations,
               [x IN incoming WHERE x.source IS NOT NULL] AS incoming_relations
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, name=name, project=project)
            record = await result.single()
            if not record:
                return {}
            return {
                "entity": dict(record["entity"]),
                "outgoing_relations": record["outgoing_relations"],
                "incoming_relations": record["incoming_relations"],
            }

    async def search(self, query_text: str, project: str | None = None) -> list[dict[str, Any]]:
        """Search entities by name or observations (case-insensitive contains)."""
        project_filter = "AND e.project = $project" if project else ""
        query = f"""
        MATCH (e:Entity)
        WHERE (e.name CONTAINS $query OR
               any(obs IN e.observations WHERE obs CONTAINS $query))
              {project_filter}
        RETURN e{{.*, labels: labels(e)}} AS entity
        ORDER BY e.updated_at DESC
        LIMIT 25
        """
        params: dict[str, Any] = {"query": query_text}
        if project:
            params["project"] = project
        async with self._drv.session(database=self.database) as session:
            # Pass params as `parameters=` (not `**params`): the search text is
            # bound to a parameter literally named `query`, which would collide
            # with `session.run`'s positional `query` (the Cypher string).
            result = await session.run(query, parameters=params)
            return [dict(record["entity"]) async for record in result]

    async def get_project_graph(self, project: str) -> dict[str, Any]:
        """Get the full knowledge graph for a project."""
        query = """
        MATCH (e:Entity {project: $project})
        OPTIONAL MATCH (e)-[r]->(t:Entity {project: $project})
        RETURN collect(DISTINCT e{.name, .type, .observations}) AS entities,
               collect(DISTINCT {
                   from: e.name, to: t.name, type: type(r)
               }) AS relationships
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, project=project)
            record = await result.single()
            if not record:
                return {"project": project, "entities": [], "relationships": []}
            rels = [r for r in record["relationships"] if r["to"] is not None]
            return {
                "project": project,
                "entities": record["entities"],
                "relationships": rels,
            }

    async def list_projects(self) -> list[str]:
        """List all projects in the knowledge graph."""
        query = """
        MATCH (e:Entity)
        RETURN DISTINCT e.project AS project
        ORDER BY project
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query)
            return [record["project"] async for record in result]

    # ── Migrations ────────────────────────────────────────────────────

    async def add_migration(
        self,
        project: str,
        description: str,
        cypher_up: str,
        cypher_down: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Record a schema/data migration for a project."""
        query = """
        MATCH (latest:Migration {project: $project})
        WITH max(latest.seq) AS max_seq
        WITH coalesce(max_seq, 0) + 1 AS next_seq
        CREATE (m:Migration {
            project: $project,
            seq: next_seq,
            version: coalesce($version, toString(next_seq)),
            description: $description,
            cypher_up: $cypher_up,
            cypher_down: $cypher_down,
            created_at: datetime(),
            applied: false
        })
        RETURN m{.*} AS migration
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(
                query,
                project=project,
                description=description,
                cypher_up=cypher_up,
                cypher_down=cypher_down,
                version=version,
            )
            record = await result.single()
            return dict(record["migration"]) if record else {}

    async def get_migrations(self, project: str) -> list[dict[str, Any]]:
        """Get migration history for a project."""
        query = """
        MATCH (m:Migration {project: $project})
        RETURN m{.*} AS migration
        ORDER BY m.seq
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, project=project)
            return [dict(record["migration"]) async for record in result]

    async def apply_migration(self, project: str, seq: int) -> dict[str, Any]:
        """Execute a migration's cypher_up and mark it as applied."""
        get_query = """
        MATCH (m:Migration {project: $project, seq: $seq, applied: false})
        RETURN m{.*} AS migration
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(get_query, project=project, seq=seq)
            record = await result.single()
            if not record:
                raise MigrationNotFoundError(project, seq)

            migration = dict(record["migration"])
            await session.run(migration["cypher_up"])
            await session.run(
                "MATCH (m:Migration {project: $project, seq: $seq}) "
                "SET m.applied = true, m.applied_at = datetime()",
                project=project,
                seq=seq,
            )
            migration["applied"] = True
            return migration

    # ── Raw Cypher ────────────────────────────────────────────────────

    async def run_cypher(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute an arbitrary Cypher query."""
        async with self._drv.session(database=self.database) as session:
            result = await session.run(query, parameters=params or {})
            return [dict(record) async for record in result]

    # ── Scored search ─────────────────────────────────────────────────

    async def vector_search(
        self, label: str, prop: str, query: list[float], k: int = 10
    ) -> list[dict[str, Any]]:
        """Scored approximate-nearest-neighbour search over an embedding property.

        Wraps drevo's ``CALL drevo.vector.query(label, property, query, k)``
        procedure (drevo issue #202): ``label``/``prop`` select the node label
        and its embedding property, ``query`` is the query vector, ``k`` the
        number of neighbours. Returns the top-``k`` nodes with their similarity
        ``score``, best-first — each row ``{"node": {...}, "score": float}``.
        """
        cypher = """
        CALL drevo.vector.query($label, $prop, $query, $k) YIELD node, score
        RETURN node{.*, labels: labels(node)} AS node, score
        ORDER BY score DESC
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(
                cypher,
                parameters={"label": label, "prop": prop, "query": query, "k": k},
            )
            return [
                {"node": dict(record["node"]), "score": record["score"]} async for record in result
            ]

    async def fts_search(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        """Scored BM25 full-text search over indexed node text.

        Wraps drevo's ``CALL fts.search(query, k)`` procedure (drevo issue
        #208): ``query`` is the search text, ``k`` the number of results.
        Returns the top-``k`` matching nodes with their BM25 ``score``,
        best-first — each row ``{"node": {...}, "score": float}``.
        """
        cypher = """
        CALL fts.search($query, $k) YIELD node, score
        RETURN node{.*, labels: labels(node)} AS node, score
        ORDER BY score DESC
        """
        async with self._drv.session(database=self.database) as session:
            result = await session.run(cypher, parameters={"query": query, "k": k})
            return [
                {"node": dict(record["node"]), "score": record["score"]} async for record in result
            ]

    # ── Embeddings (drevo /v1/embeddings, issue #217) ─────────────────

    async def embed_text(self, text: str, model: str | None = None) -> list[float]:
        """Turn ``text`` into an embedding vector via drevo's OpenAI-compatible
        ``POST /v1/embeddings``.

        drevo proxies the request to its configured upstream (OpenAI / Voyage /
        Ollama / …), so one drevo instance is the whole RAG backend. ``model``
        is optional — when omitted, drevo fills in its configured default.

        Requires drevo built with the ``embeddings-proxy`` feature and
        ``DREVO_EMBEDDINGS_UPSTREAM`` set; otherwise drevo answers ``503`` and
        this raises :class:`EmbeddingError`. Only float-vector responses are
        supported (do not request ``encoding_format: "base64"``).
        """
        if not text:
            raise EmbeddingError("cannot embed empty text")
        payload: dict[str, Any] = {"input": text}
        if model:
            payload["model"] = model
        url = f"{self.http_url.rstrip('/')}/v1/embeddings"
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embeddings request to {url} failed: {exc}") from exc
        if response.status_code == 503:
            raise EmbeddingError(
                "drevo embeddings backend not configured (503) — build drevo with "
                "--features embeddings-proxy and set DREVO_EMBEDDINGS_UPSTREAM"
            )
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embeddings upstream error {response.status_code}: {response.text[:200]}"
            )
        try:
            vector = response.json()["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"unexpected embeddings response shape: {exc}") from exc
        if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
            raise EmbeddingError(
                "embedding is not a float vector (base64 encoding_format is not "
                "supported by semantic_search)"
            )
        return [float(x) for x in vector]

    async def semantic_search(
        self,
        query: str,
        label: str,
        prop: str = "embedding",
        k: int = 10,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Embed ``query`` (via :meth:`embed_text`) then vector-search it against
        ``label``.``prop`` (via :meth:`vector_search`).

        Text-in, ranked-nodes-out: the self-contained RAG path where a single
        drevo instance provides graph, vectors, **and** embedding generation.
        Returns the top-``k`` nodes with their similarity ``score``, best-first
        — each row ``{"node": {...}, "score": float}``.
        """
        vector = await self.embed_text(query, model=model)
        return await self.vector_search(label, prop, vector, k)
