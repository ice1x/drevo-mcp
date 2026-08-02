#!/usr/bin/env python3
"""Backfill semantic-search embeddings onto existing graph nodes.

For every ``:Entity`` node that has no ``embedding`` yet, embed its
``name`` + ``body`` via drevo's own OpenAI-compatible ``POST /v1/embeddings``
proxy and ``SET n.embedding = <float vector>``. That is all
``CALL drevo.vector.query`` / ``semantic_search`` need — the query scans the
property and ranks by cosine similarity (no separate index to build).

Idempotent and resumable: it only touches nodes where ``n.embedding IS NULL``,
so a re-run embeds just the new/unfinished ones. Safe to interrupt.

Config (env, with defaults for the standard local deploy):
  DREVO_BOLT_URL   bolt://localhost:7688   Bolt endpoint (the graph)
  DREVO_HTTP_URL   http://localhost:8080   HTTP base (/v1/embeddings lives here)
  DREVO_LABEL      Entity                  node label to embed
  DREVO_EMB_PROP   embedding               property to write the vector to
  DREVO_EMB_MODEL  (unset)                 optional model override

Usage:
  python scripts/backfill-embeddings.py [LIMIT]   # LIMIT omitted/0 = all
"""

from __future__ import annotations

import os
import sys

import httpx
from neo4j import GraphDatabase

BOLT = os.environ.get("DREVO_BOLT_URL", "bolt://localhost:7688")
HTTP = os.environ.get("DREVO_HTTP_URL", "http://localhost:8080").rstrip("/")
LABEL = os.environ.get("DREVO_LABEL", "Entity")
PROP = os.environ.get("DREVO_EMB_PROP", "embedding")
MODEL = os.environ.get("DREVO_EMB_MODEL") or None
BATCH = 32  # texts per /v1/embeddings request
MAXCHARS = 8000  # cap per node (~2k tokens; stays under the 8k-token input limit)


def fetch(tx, limit):
    q = (
        f"MATCH (n:{LABEL}) WHERE n.{PROP} IS NULL "
        "RETURN id(n) AS id, coalesce(n.name,'') AS name, coalesce(n.body,'') AS body"
    )
    if limit:
        q += f" LIMIT {limit}"
    out = []
    for r in tx.run(q):
        text = ((r["name"] or "") + "\n" + (r["body"] or "")).strip()[:MAXCHARS]
        out.append((r["id"], text or "(empty)"))
    return out


def embed(texts):
    payload = {"input": texts}
    if MODEL:
        payload["model"] = MODEL
    r = httpx.post(f"{HTTP}/v1/embeddings", json=payload, timeout=180)
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    driver = GraphDatabase.driver(BOLT)
    try:
        with driver.session() as session:
            nodes = session.execute_read(fetch, limit)
            print(f"to embed: {len(nodes)} {LABEL} node(s) missing {PROP}")
            done = 0
            for i in range(0, len(nodes), BATCH):
                chunk = nodes[i : i + BATCH]
                vecs = embed([t for _, t in chunk])
                rows = [{"id": nid, "vec": v} for (nid, _), v in zip(chunk, vecs)]
                session.execute_write(
                    lambda tx, rows=rows: tx.run(
                        f"UNWIND $rows AS r MATCH (n) WHERE id(n)=r.id SET n.{PROP}=r.vec",
                        rows=rows,
                    ).consume()
                )
                done += len(chunk)
                print(f"  embedded {done}/{len(nodes)}")
        print("done")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
