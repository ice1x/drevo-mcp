#!/usr/bin/env bash
#
# backfill-fts-body.sh — one-shot: make EXISTING entities full-text searchable.
#
# drevo's `CALL fts.search` indexes a node's `title` + `body`, NOT its arbitrary
# properties. Entities written before `create_entity` began mirroring
# `name` + `observations` into `body` (see drevo_mcp_bolt/graph.py) are
# therefore invisible to `fts_search`. This backfills `body` for every
# `:Entity`, which re-indexes it in drevo's FTS. It is **idempotent** — re-running
# recomputes the same `body` — and additive (no data is removed), so it is safe
# to run against a live graph. Back up the redb file first if you want a
# rollback point.
#
# Usage:
#   ./scripts/backfill-fts-body.sh                        # http://localhost:8080
#   DREVO_HTTP_URL=http://host:8080 ./scripts/backfill-fts-body.sh
#
set -euo pipefail

URL="${DREVO_HTTP_URL:-http://localhost:8080}/cypher"

# Single-line Cypher (uses only single-quoted string literals, so it embeds in
# JSON without any double-quote escaping). `reduce` joins the observations list;
# `coalesce` handles entities that have none.
#
# The WHERE guard makes this SAFE and idempotent: it only backfills entities
# that have no `body` yet (the ones written via Cypher, which drevo never
# FTS-indexed). Entities that already carry a `body` — e.g. natively-created
# nodes that are already searchable — are left untouched, so their existing
# indexed text is never clobbered.
QUERY="MATCH (e:Entity) WHERE e.body IS NULL OR e.body = '' SET e.body = e.name + ' ' + reduce(acc = '', o IN coalesce(e.observations, []) | acc + ' ' + o) RETURN count(e) AS migrated"

command -v curl >/dev/null 2>&1 || { echo "backfill: curl not found on PATH" >&2; exit 1; }

echo "Backfilling :Entity.body for full-text search at ${URL} …"
response=$(curl -fsS -X POST "${URL}" -H 'content-type: application/json' \
  --data "{\"query\": \"${QUERY}\"}")

echo "${response}"
echo "Done. Every :Entity is now FTS-indexed — fts_search finds them."
