"""Single-signal retrievers: full-text (SQLite FTS5/BM25) and vector (k-NN)."""
from __future__ import annotations

import logging
import sqlite3

from engram.core.db import Database
from engram.retrieval.query import ParsedQuery

log = logging.getLogger("engram.search")


def fts_search(db: Database, pq: ParsedQuery, limit: int = 50) -> list[tuple[int, float, str]]:
    """Return (engram_id, bm25 score — higher is better, highlighted snippet)."""
    expr = pq.fts_query
    if not expr:
        return []
    try:
        rows = db.query(
            "SELECT f.rowid AS id, -bm25(engrams_fts, 1.0, 0.4) AS score, "
            "snippet(engrams_fts, 0, '⟦', '⟧', ' … ', 18) AS snip "
            "FROM engrams_fts f JOIN engrams g ON g.id = f.rowid "
            "WHERE engrams_fts MATCH ? AND g.status='active' ORDER BY bm25(engrams_fts, 1.0, 0.4) LIMIT ?",
            (expr, limit),
        )
    except sqlite3.OperationalError as exc:  # should not happen: terms are sanitised
        log.warning("FTS query failed (%s) for %r", exc, expr)
        return []
    return [(r["id"], float(r["score"]), r["snip"]) for r in rows]


def vector_search(vectors, provider, query: str, limit: int = 50) -> list[tuple[int, float]]:
    if vectors is None or provider is None:
        return []
    qv = provider.embed([query], mode="query")[0]
    if not float((qv * qv).sum()) > 0:  # no features (e.g. punctuation only): no meaningful neighbours
        return []
    return vectors.knn(qv, k=limit)
