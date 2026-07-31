"""Locks the contract of ``scripts/backfill-fts-body.sh``.

The script is a one-shot that makes existing ``:Entity`` nodes full-text
searchable by mirroring ``name`` + ``observations`` into the drevo-indexed
``body`` field. We can't run drevo in CI, so these are text-level assertions
(mirroring ``test_run_script.py``): the script's basic shape plus the invariants
that make it correct and safe (targets ``:Entity``, sets ``body``, idempotent
via a pure recompute, posts to the ``/cypher`` endpoint).
"""

from __future__ import annotations

import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill-fts-body.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"expected {SCRIPT} to exist"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "backfill-fts-body.sh must be executable"


def test_script_has_bash_shebang_and_is_strict() -> None:
    text = _text()
    first = text.splitlines()[0]
    assert first.startswith("#!") and "bash" in first, "expected a bash shebang"
    assert "set -euo pipefail" in text, "script must fail fast (set -euo pipefail)"


def test_backfills_entity_body_for_fts() -> None:
    text = _text()
    assert "MATCH (e:Entity)" in text, "must target every :Entity node"
    assert "SET e.body" in text, "must write the FTS-indexed `body` field"
    # body is derived from name + observations, so re-running is idempotent.
    assert "e.name" in text and "e.observations" in text, "body must mirror name + observations"
    assert "reduce(" in text, "observations (a list) must be joined into body text"


def test_targets_the_cypher_endpoint() -> None:
    text = _text()
    assert "/cypher" in text, "must POST the migration to the drevo /cypher endpoint"
    assert "DREVO_HTTP_URL" in text, "the drevo endpoint must be overridable via DREVO_HTTP_URL"
