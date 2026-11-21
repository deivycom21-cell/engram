"""Persistence for documents, engrams, concepts, relations, insights, jobs and the audit log.

Invariant: nothing in this module deletes an engram or a document. Consolidation can
*propose* relations and insights; users confirm or reject them. Rejected relations are
kept (status='rejected') so they are not proposed again.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from engram.core.db import Database
from engram.memory.models import (
    Document, Engram, Relation, RelationOrigin, RelationStatus, RelationType, new_uid, now_iso,
)

ENGRAM_SELECT = (
    "SELECT g.*, d.uid AS doc_uid, d.title AS doc_title, d.filename AS doc_filename, d.sha256 AS doc_sha256 "
    "FROM engrams g LEFT JOIN documents d ON d.id = g.document_id"
)

SYMMETRIC = {
    RelationType.SEMANTICALLY_SIMILAR.value, RelationType.SHARES_CONCEPTS.value,
    RelationType.POSSIBLE_DUPLICATE.value, RelationType.POSSIBLE_CONTRADICTION.value,
}


class MemoryStore:
    def __init__(self, db: Database, embedding_model: str | None = None):
        self.db = db
        self.embedding_model = embedding_model

    # ================================================================ documents
    def document_by_sha(self, sha256: str) -> Document | None:
        row = self.db.query_one("SELECT * FROM documents WHERE sha256=?", (sha256,))
        return Document.from_row(row) if row else None

    def latest_document_by_name(self, filename: str) -> Document | None:
        row = self.db.query_one(
            "SELECT * FROM documents WHERE filename=? AND status='active' ORDER BY id DESC LIMIT 1", (filename,)
        )
        return Document.from_row(row) if row else None

    def insert_document(self, conn: sqlite3.Connection, **f: Any) -> int:
        cur = conn.execute(
            "INSERT INTO documents(uid,title,filename,original_path,stored_path,sha256,kind,size_bytes,authored_at,"
            "authored_at_source,file_modified_at,ingested_at,previous_version_id,metadata,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_uid("doc"), f["title"], f["filename"], f.get("original_path"), f["stored_path"], f["sha256"],
                f["kind"], f["size_bytes"], f.get("authored_at"), f.get("authored_at_source"),
                f.get("file_modified_at"), now_iso(), f.get("previous_version_id"),
                json.dumps(f.get("metadata") or {}, ensure_ascii=False, default=str), f.get("status", "active"),
            ),
        )
        return int(cur.lastrowid)

    def get_document(self, doc_id: int) -> Document | None:
        row = self.db.query_one(
            "SELECT d.*, (SELECT count(*) FROM engrams g WHERE g.document_id=d.id) AS engram_count "
            "FROM documents d WHERE d.id=?",
            (doc_id,),
        )
        return Document.from_row(row) if row else None

    def list_documents(self) -> list[Document]:
        rows = self.db.query(
            "SELECT d.*, (SELECT count(*) FROM engrams g WHERE g.document_id=d.id) AS engram_count "
            "FROM documents d ORDER BY d.ingested_at DESC, d.id DESC"
        )
        return [Document.from_row(r) for r in rows]

    def document_by_title(self, title: str) -> Document | None:
        from engram.core.textutil import norm

        target = norm(title)
        for d in self.list_documents():
            if d.status == "active" and (norm(d.title) == target or norm(d.filename.rsplit(".", 1)[0]) == target):
                return d
        return None

    # ================================================================ engrams
    def insert_engram(self, conn: sqlite3.Connection, **f: Any) -> int:
        cur = conn.execute(
            "INSERT INTO engrams(uid,document_id,kind,kind_confidence,kind_reason,title,content,content_hash,position,"
            "page,char_start,char_end,occurred_at,occurred_at_source,created_at,created_at_source,ingested_at,"
            "confidence,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_uid("eg"), f.get("document_id"), f["kind"], f.get("kind_confidence"), f.get("kind_reason"),
                f.get("title"), f["content"], f["content_hash"], f.get("position"), f.get("page"),
                f.get("char_start"), f.get("char_end"), f.get("occurred_at"), f.get("occurred_at_source"),
                f.get("created_at"), f.get("created_at_source"), now_iso(), f.get("confidence"),
                json.dumps(f.get("metadata") or {}, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)

    def _concepts_for(self, ids: list[int]) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {i: [] for i in ids}
        for part in _chunks(ids, 500):
            marks = ",".join("?" * len(part))
            for r in self.db.query(
                f"SELECT ec.engram_id, c.id, c.name, c.kind, ec.weight, ec.origin FROM engram_concepts ec "
                f"JOIN concepts c ON c.id=ec.concept_id WHERE ec.engram_id IN ({marks}) ORDER BY ec.weight DESC",
                part,
            ):
                out[r["engram_id"]].append(
                    {"id": r["id"], "name": r["name"], "kind": r["kind"], "weight": r["weight"], "origin": r["origin"]}
                )
        return out

    def _embedded(self, ids: list[int]) -> set[int]:
        if not self.embedding_model or not ids:
            return set()
        got: set[int] = set()
        for part in _chunks(ids, 500):
            marks = ",".join("?" * len(part))
            got |= {
                r["engram_id"]
                for r in self.db.query(
                    f"SELECT engram_id FROM embeddings WHERE model=? AND engram_id IN ({marks})",
                    (self.embedding_model, *part),
                )
            }
        return got

    def get_engram(self, engram_id: int) -> Engram | None:
        res = self.get_engrams([engram_id])
        return res[0] if res else None

    def get_engrams(self, ids: Iterable[int], with_concepts: bool = True) -> list[Engram]:
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            return []
        rows = []
        for part in _chunks(ids, 500):
            marks = ",".join("?" * len(part))
            rows += self.db.query(f"{ENGRAM_SELECT} WHERE g.id IN ({marks})", part)
        concepts = self._concepts_for(ids) if with_concepts else {}
        embedded = self._embedded(ids)
        by_id = {r["id"]: Engram.from_row(r, concepts.get(r["id"]), r["id"] in embedded) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def list_engrams(
        self, kinds: list[str] | None = None, document_id: int | None = None, limit: int = 100, offset: int = 0,
        order: str = "recent",
    ) -> tuple[list[Engram], int]:
        where, params = ["g.status='active'"], []
        if kinds:
            where.append(f"g.kind IN ({','.join('?' * len(kinds))})")
            params += kinds
        if document_id is not None:
            where.append("g.document_id=?")
            params.append(document_id)
        clause = " AND ".join(where)
        order_sql = {
            "recent": "g.ingested_at DESC, g.id DESC",
            "position": "g.document_id, g.position",
            "date": "coalesce(g.occurred_at, g.created_at) IS NULL, coalesce(g.occurred_at, g.created_at), g.id",
        }.get(order, "g.id DESC")
        total = self.db.query_one(f"SELECT count(*) AS n FROM engrams g WHERE {clause}", params)["n"]
        rows = self.db.query(f"SELECT g.id FROM engrams g WHERE {clause} ORDER BY {order_sql} LIMIT ? OFFSET ?", (*params, limit, offset))
        return self.get_engrams([r["id"] for r in rows]), int(total)

    def engram_ids_by_hash(self, content_hash: str) -> list[int]:
        return [r["id"] for r in self.db.query("SELECT id FROM engrams WHERE content_hash=? AND status='active'", (content_hash,))]

    def touch(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        ts = now_iso()
        with self.db.lock:
            self.db.conn.executemany(
                "UPDATE engrams SET access_count=access_count+1, last_accessed_at=? WHERE id=?", [(ts, i) for i in ids]
            )

    def set_engram_kind(self, engram_id: int, kind: str) -> None:
        self.db.execute(
            "UPDATE engrams SET kind=?, kind_confidence=1.0, kind_reason='set by user' WHERE id=?", (kind, engram_id)
        )
        self.audit("engram.kind_changed", {"engram_id": engram_id, "kind": kind})

    def set_engram_status(self, engram_id: int, status: str) -> None:
        """Archive/restore. Archiving hides from search but never deletes the content."""
        self.db.execute("UPDATE engrams SET status=? WHERE id=?", (status, engram_id))
        self.audit("engram.status_changed", {"engram_id": engram_id, "status": status})

    # ================================================================ concepts
    def upsert_concept(self, conn: sqlite3.Connection, name: str, key: str, kind: str) -> int:
        row = conn.execute("SELECT id FROM concepts WHERE norm=? AND kind=?", (key, kind)).fetchone()
        if row:
            return int(row[0])
        return int(conn.execute("INSERT INTO concepts(name,norm,kind) VALUES (?,?,?)", (name, key, kind)).lastrowid)

    def link_concept(self, conn: sqlite3.Connection, engram_id: int, concept_id: int, weight: float, origin: str) -> None:
        conn.execute(
            "INSERT INTO engram_concepts(engram_id,concept_id,weight,origin) VALUES (?,?,?,?) "
            "ON CONFLICT(engram_id,concept_id) DO UPDATE SET weight=max(weight, excluded.weight)",
            (engram_id, concept_id, weight, origin),
        )

    def concept_document_frequency(self) -> dict[int, int]:
        """Number of distinct documents in which each concept appears (for IDF weighting)."""
        rows = self.db.query(
            "SELECT ec.concept_id AS cid, count(DISTINCT g.document_id) AS n FROM engram_concepts ec "
            "JOIN engrams g ON g.id=ec.engram_id WHERE g.status='active' GROUP BY ec.concept_id"
        )
        return {r["cid"]: r["n"] for r in rows}

    def top_concepts(self, limit: int = 30) -> list[dict]:
        rows = self.db.query(
            "SELECT c.id, c.name, c.kind, count(DISTINCT ec.engram_id) AS engrams, count(DISTINCT g.document_id) AS documents "
            "FROM concepts c JOIN engram_concepts ec ON ec.concept_id=c.id JOIN engrams g ON g.id=ec.engram_id "
            "WHERE g.status='active' GROUP BY c.id HAVING engrams >= 2 ORDER BY documents DESC, engrams DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def search_concepts(self, q: str, limit: int = 20) -> list[dict]:
        from engram.core.textutil import norm

        rows = self.db.query(
            "SELECT c.id, c.name, c.kind, count(ec.engram_id) AS engrams FROM concepts c "
            "JOIN engram_concepts ec ON ec.concept_id=c.id WHERE c.norm LIKE ? GROUP BY c.id ORDER BY engrams DESC LIMIT ?",
            (f"%{norm(q)}%", limit),
        )
        return [dict(r) for r in rows]

    def engrams_with_concept(self, concept_id: int) -> list[int]:
        return [r["engram_id"] for r in self.db.query(
            "SELECT ec.engram_id FROM engram_concepts ec JOIN engrams g ON g.id=ec.engram_id "
            "WHERE ec.concept_id=? AND g.status='active'", (concept_id,))]

    # ================================================================ relations
    def add_relation(
        self, source_id: int, target_id: int, rtype: RelationType, origin: RelationOrigin, explanation: str,
        confidence: float, detector: str, evidence: dict | None = None, conn: sqlite3.Connection | None = None,
    ) -> int | None:
        """Insert a relation unless it (or its rejected twin) already exists. Returns new id or None."""
        if source_id == target_id:
            return None
        if rtype.value in SYMMETRIC and source_id > target_id:
            source_id, target_id = target_id, source_id
        status = RelationStatus.PROPOSED if origin == RelationOrigin.INFERRED else RelationStatus.CONFIRMED
        c = conn or self.db.conn
        with self.db.lock:
            cur = c.execute(
                "INSERT OR IGNORE INTO relations(source_id,target_id,type,origin,explanation,confidence,evidence,detector,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id, target_id, rtype.value, origin.value, explanation, round(float(confidence), 4),
                    json.dumps(evidence or {}, ensure_ascii=False, default=str), detector, status.value, now_iso(),
                ),
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def relations_for(self, engram_id: int, include_rejected: bool = False) -> list[Relation]:
        extra = "" if include_rejected else " AND status!='rejected'"
        rows = self.db.query(
            f"SELECT * FROM relations WHERE (source_id=? OR target_id=?){extra} ORDER BY origin, confidence DESC",
            (engram_id, engram_id),
        )
        return [Relation.from_row(r) for r in rows]

    def relations_among(self, ids: list[int], include_structural: bool = True) -> list[Relation]:
        if not ids:
            return []
        out: list[Relation] = []
        idset = set(ids)
        for part in _chunks(ids, 400):
            marks = ",".join("?" * len(part))
            rows = self.db.query(
                f"SELECT * FROM relations WHERE status!='rejected' AND source_id IN ({marks})", part
            )
            out += [Relation.from_row(r) for r in rows if r["target_id"] in idset]
        if not include_structural:
            out = [r for r in out if r.origin != RelationOrigin.STRUCTURAL]
        return out

    def list_relations(self, types: list[str] | None = None, status: str | None = None, origin: str | None = None,
                       limit: int = 200) -> list[Relation]:
        where, params = ["1=1"], []
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            params += types
        if status:
            where.append("status=?")
            params.append(status)
        if origin:
            where.append("origin=?")
            params.append(origin)
        rows = self.db.query(
            f"SELECT * FROM relations WHERE {' AND '.join(where)} ORDER BY confidence DESC, id DESC LIMIT ?", (*params, limit)
        )
        return [Relation.from_row(r) for r in rows]

    def get_relation(self, rel_id: int) -> Relation | None:
        row = self.db.query_one("SELECT * FROM relations WHERE id=?", (rel_id,))
        return Relation.from_row(row) if row else None

    def set_relation_status(self, rel_id: int, status: RelationStatus) -> Relation | None:
        self.db.execute("UPDATE relations SET status=? WHERE id=?", (status.value, rel_id))
        self.audit("relation.status_changed", {"relation_id": rel_id, "status": status.value})
        return self.get_relation(rel_id)

    # ================================================================ insights
    def upsert_insight(self, kind: str, title: str, explanation: str, engram_ids: list[int], score: float,
                       fingerprint: str) -> bool:
        """Create or refresh an insight. Dismissed/accepted insights keep their status."""
        ts = now_iso()
        with self.db.lock:
            row = self.db.conn.execute("SELECT id FROM insights WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row:
                self.db.conn.execute(
                    "UPDATE insights SET title=?, explanation=?, engram_ids=?, score=?, updated_at=? WHERE id=?",
                    (title, explanation, json.dumps(engram_ids), score, ts, row[0]),
                )
                return False
            self.db.conn.execute(
                "INSERT INTO insights(kind,title,explanation,engram_ids,score,status,fingerprint,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'open',?,?,?)",
                (kind, title, explanation, json.dumps(engram_ids), score, fingerprint, ts, ts),
            )
            return True

    def list_insights(self, status: str | None = "open", kind: str | None = None, limit: int = 100) -> list[dict]:
        where, params = ["1=1"], []
        if status:
            where.append("status=?")
            params.append(status)
        if kind:
            where.append("kind=?")
            params.append(kind)
        rows = self.db.query(
            f"SELECT * FROM insights WHERE {' AND '.join(where)} ORDER BY score DESC, id DESC LIMIT ?", (*params, limit)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["engram_ids"] = json.loads(d["engram_ids"])
            out.append(d)
        return out

    def set_insight_status(self, insight_id: int, status: str) -> None:
        self.db.execute("UPDATE insights SET status=?, updated_at=? WHERE id=?", (status, now_iso(), insight_id))
        self.audit("insight.status_changed", {"insight_id": insight_id, "status": status})

    # ================================================================ jobs & audit
    def create_job(self, kind: str, payload: dict) -> int:
        ts = now_iso()
        cur = self.db.execute(
            "INSERT INTO jobs(kind,status,payload,created_at,updated_at) VALUES (?,?,?,?,?)",
            (kind, "queued", json.dumps(payload, ensure_ascii=False, default=str), ts, ts),
        )
        return int(cur.lastrowid)

    def update_job(self, job_id: int, status: str, result: Any = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE jobs SET status=?, result=?, error=?, updated_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None, error, now_iso(), job_id),
        )

    def get_job(self, job_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            return None
        d = dict(row)
        for k in ("payload", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        return [self.get_job(r["id"]) for r in self.db.query("SELECT id FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def audit(self, action: str, detail: dict) -> None:
        self.db.execute(
            "INSERT INTO audit_log(at, action, detail) VALUES (?,?,?)",
            (now_iso(), action, json.dumps(detail, ensure_ascii=False, default=str)),
        )

    def recent_activity(self, limit: int = 30) -> list[dict]:
        rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ================================================================ stats
    def stats(self) -> dict:
        q = lambda sql, p=(): self.db.query_one(sql, p)[0]  # noqa: E731
        kinds = {r["kind"]: r["n"] for r in self.db.query(
            "SELECT kind, count(*) AS n FROM engrams WHERE status='active' GROUP BY kind")}
        rel_types = {f'{r["type"]}:{r["status"]}': r["n"] for r in self.db.query(
            "SELECT type, status, count(*) AS n FROM relations GROUP BY type, status")}
        return {
            "documents": q("SELECT count(*) FROM documents"),
            "engrams": q("SELECT count(*) FROM engrams WHERE status='active'"),
            "archived_engrams": q("SELECT count(*) FROM engrams WHERE status!='active'"),
            "concepts": q("SELECT count(*) FROM concepts"),
            "relations": q("SELECT count(*) FROM relations WHERE status!='rejected'"),
            "relations_by_type": rel_types,
            "inferred_relations_pending": q("SELECT count(*) FROM relations WHERE origin='inferred' AND status='proposed'"),
            "embeddings": q("SELECT count(*) FROM embeddings WHERE model=?", (self.embedding_model or "",)),
            "insights_open": q("SELECT count(*) FROM insights WHERE status='open'"),
            "engrams_by_kind": kinds,
            "dated_engrams": q("SELECT count(*) FROM engrams WHERE status='active' AND coalesce(occurred_at, created_at) IS NOT NULL"),
            "first_date": q("SELECT min(coalesce(occurred_at, created_at)) FROM engrams WHERE status='active'"),
            "last_date": q("SELECT max(coalesce(occurred_at, created_at)) FROM engrams WHERE status='active'"),
        }


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
