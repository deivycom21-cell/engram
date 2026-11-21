"""Vector memory: stores embeddings and answers k-nearest-neighbour queries.

The ``embeddings`` table is the source of truth (one row per engram *and model*).
The sqlite-vec ``vec_index`` virtual table is a derived index for the active model and
is rebuilt automatically when the model changes. Without sqlite-vec, an in-memory
numpy matrix (lazily loaded, invalidated on writes) gives exact brute-force search.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from engram.core.db import Database
from engram.memory.embeddings import from_blob, to_blob

log = logging.getLogger("engram.vectors")


class VectorIndex:
    def __init__(self, db: Database, model: str, dim: int):
        self.db = db
        self.model = model
        self.dim = dim
        self.use_vec = db.vec_available
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None
        self._ids: np.ndarray | None = None
        if self.use_vec:
            self._ensure_vec_table()

    # ------------------------------------------------------------------ setup
    def _ensure_vec_table(self) -> None:
        current_model = self.db.get_meta("vec_model")
        current_dim = self.db.get_meta("vec_dim")
        exists = self.db.query_one("SELECT 1 FROM sqlite_master WHERE name='vec_index'") is not None
        if exists and current_model == self.model and current_dim == str(self.dim):
            return
        log.info("(re)building vec_index for %s (dim=%d)", self.model, self.dim)
        with self.db.transaction() as c:
            c.execute("DROP TABLE IF EXISTS vec_index")  # derived data only; embeddings table is untouched
            c.execute(f"CREATE VIRTUAL TABLE vec_index USING vec0(embedding float[{int(self.dim)}] distance_metric=cosine)")
            c.execute(
                "INSERT INTO vec_index(rowid, embedding) SELECT engram_id, vector FROM embeddings WHERE model=? AND dim=?",
                (self.model, self.dim),
            )
            c.execute("INSERT INTO meta(key,value) VALUES('vec_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.model,))
            c.execute("INSERT INTO meta(key,value) VALUES('vec_dim',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.dim),))

    # ------------------------------------------------------------------ writes
    def add(self, conn, engram_id: int, vector: np.ndarray) -> None:
        """Insert inside an existing transaction (``conn`` from ``db.transaction()``)."""
        blob = to_blob(vector)
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(engram_id, model, dim, vector) VALUES (?,?,?,?)",
            (engram_id, self.model, self.dim, blob),
        )
        if self.use_vec:
            conn.execute("DELETE FROM vec_index WHERE rowid=?", (engram_id,))
            conn.execute("INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)", (engram_id, blob))
        self.invalidate()

    def invalidate(self) -> None:
        with self._lock:
            self._matrix = None
            self._ids = None

    # ------------------------------------------------------------------ reads
    def get(self, engram_id: int) -> np.ndarray | None:
        row = self.db.query_one("SELECT vector FROM embeddings WHERE engram_id=? AND model=?", (engram_id, self.model))
        return from_blob(row["vector"]) if row else None

    def get_many(self, ids: list[int]) -> dict[int, np.ndarray]:
        if not ids:
            return {}
        out: dict[int, np.ndarray] = {}
        for i in range(0, len(ids), 500):
            part = ids[i : i + 500]
            marks = ",".join("?" * len(part))
            for r in self.db.query(
                f"SELECT engram_id, vector FROM embeddings WHERE model=? AND engram_id IN ({marks})", (self.model, *part)
            ):
                out[r["engram_id"]] = from_blob(r["vector"])
        return out

    def count(self) -> int:
        row = self.db.query_one("SELECT count(*) AS n FROM embeddings WHERE model=?", (self.model,))
        return int(row["n"]) if row else 0

    def _load_matrix(self) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._matrix is None:
                rows = self.db.query(
                    "SELECT e.engram_id, e.vector FROM embeddings e JOIN engrams g ON g.id=e.engram_id "
                    "WHERE e.model=? AND g.status='active' ORDER BY e.engram_id",
                    (self.model,),
                )
                if rows:
                    self._ids = np.array([r["engram_id"] for r in rows], dtype=np.int64)
                    self._matrix = np.vstack([from_blob(r["vector"]) for r in rows])
                else:
                    self._ids = np.zeros((0,), dtype=np.int64)
                    self._matrix = np.zeros((0, self.dim), dtype=np.float32)
            return self._ids, self._matrix

    def knn(self, vector: np.ndarray, k: int = 10, exclude: set[int] | None = None) -> list[tuple[int, float]]:
        """Return up to ``k`` (engram_id, cosine_similarity) pairs, best first."""
        exclude = exclude or set()
        fetch = k + len(exclude)
        if self.use_vec:
            rows = self.db.query(
                "SELECT v.rowid AS id, v.distance AS d FROM vec_index v "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (to_blob(vector), min(fetch * 2 + 10, 4096)),
            )
            active = self._active_ids([r["id"] for r in rows])
            res = [(r["id"], 1.0 - float(r["d"])) for r in rows
                   if r["d"] is not None and r["id"] not in exclude and r["id"] in active]
            return res[:k]
        ids, matrix = self._load_matrix()
        if len(ids) == 0:
            return []
        sims = matrix @ np.asarray(vector, dtype=np.float32)
        order = np.argsort(-sims)[: fetch]
        res = [(int(ids[i]), float(sims[i])) for i in order if int(ids[i]) not in exclude]
        return res[:k]

    def _active_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r["id"] for r in self.db.query(f"SELECT id FROM engrams WHERE status='active' AND id IN ({marks})", ids)}
