"""Locks the contract of ``scripts/backfill-embeddings.py``.

The script is a one-shot that makes existing ``:Entity`` nodes
semantic-searchable by embedding their text (via drevo's ``/v1/embeddings``
proxy) into an ``embedding`` vector property that ``CALL drevo.vector.query``
scans. We can't run drevo in CI, so these are text/compile-level assertions
(mirroring ``test_backfill_script.py``): the script imports cleanly and carries
the invariants that make it correct and safe (targets ``:Entity``, writes the
``embedding`` property, idempotent via ``embedding IS NULL``, embeds through
``/v1/embeddings``, batches its requests).
"""

from __future__ import annotations

import ast
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill-embeddings.py"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"expected {SCRIPT} to exist"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "backfill-embeddings.py must be executable"


def test_script_is_valid_python() -> None:
    # Parses without executing — catches syntax errors without needing a live
    # drevo or the httpx/neo4j runtime deps.
    ast.parse(_text())


def test_targets_entity_and_writes_embedding_property() -> None:
    text = _text()
    assert "MATCH (n:{LABEL})" in text or "n:Entity" in text, "must target :Entity nodes"
    assert "SET n.{PROP}=" in text, "must write the embedding vector property"


def test_writes_are_resilient() -> None:
    # drevo's Bolt intermittently fails a managed execute_write over a large
    # UNWIND batch ("no active transaction"); the script must write robustly.
    text = _text()
    assert "def write_one" in text, "per-node write helper expected"
    assert "session.run(" in text, "must write via an autocommit session.run"
    assert "range(6)" in text or "retry" in text.lower(), "must retry transient failures"


def test_is_idempotent_via_null_guard() -> None:
    # Only embeds nodes without a vector, so a re-run is resumable and cheap.
    assert "IS NULL" in _text(), "must skip already-embedded nodes (embedding IS NULL)"


def test_embeds_through_the_embeddings_proxy() -> None:
    assert "/v1/embeddings" in _text(), "must embed via drevo's /v1/embeddings proxy"


def test_batches_requests() -> None:
    text = _text()
    assert "BATCH" in text, "must batch embedding requests (a BATCH size)"
