"""Temporal memory: chronology of engrams and the evolution of a topic over time.

Dates are only those recorded in sources (content, headings, front-matter, PDF
metadata, filenames), each with its evidence. The ingestion time is shown separately
and is never presented as the date an idea was had.
"""
from __future__ import annotations

from engram.memory.models import Engram
from engram.memory.store import MemoryStore


def date_of(e: Engram) -> tuple[str | None, str | None]:
    if e.occurred_at:
        return e.occurred_at, e.occurred_at_source
    if e.created_at:
        return e.created_at, e.created_at_source
    return None, None


class TemporalMemory:
    def __init__(self, store: MemoryStore):
        self.store = store

    def timeline(self, kinds: list[str] | None = None, date_from: str | None = None, date_to: str | None = None,
                 document_id: int | None = None, limit: int = 500) -> dict:
        where, params = ["g.status='active'", "coalesce(g.occurred_at, g.created_at) IS NOT NULL"], []
        if kinds:
            where.append(f"g.kind IN ({','.join('?' * len(kinds))})")
            params += kinds
        if date_from:
            where.append("coalesce(g.occurred_at, g.created_at) >= ?")
            params.append(date_from)
        if date_to:
            where.append("coalesce(g.occurred_at, g.created_at) <= ?")
            params.append(date_to + "~")
        if document_id is not None:
            where.append("g.document_id=?")
            params.append(document_id)
        clause = " AND ".join(where)
        rows = self.store.db.query(
            f"SELECT g.id FROM engrams g WHERE {clause} ORDER BY coalesce(g.occurred_at, g.created_at), g.document_id, g.position LIMIT ?",
            (*params, limit),
        )
        undated = self.store.db.query_one(
            "SELECT count(*) FROM engrams WHERE status='active' AND coalesce(occurred_at, created_at) IS NULL")[0]
        engrams = self.store.get_engrams([r["id"] for r in rows], with_concepts=False)
        items = []
        for e in engrams:
            d, src = date_of(e)
            items.append({"date": d, "date_source": src, "engram": e.model_dump()})
        return {"items": items, "undated_count": undated, "total": len(items)}

    def ordered(self, engrams: list[Engram]) -> tuple[list[tuple[str, str, Engram]], list[Engram]]:
        dated, undated = [], []
        for e in engrams:
            d, src = date_of(e)
            if d:
                dated.append((d, src, e))
            else:
                undated.append(e)
        dated.sort(key=lambda t: (t[0], t[2].provenance.document_id or 0, t[2].provenance.position or 0))
        return dated, undated

    def evolution(self, topic: str, retriever, limit: int = 40, min_relevance: float = 0.2) -> dict:
        res = retriever.search(topic, limit=limit)
        hits = [r for r in res["results"] if r["relevance"] >= min_relevance]
        ids = [r["engram"]["id"] for r in hits]
        engrams = self.store.get_engrams(ids)
        dated, undated = self.ordered(engrams)
        rels = [
            r.model_dump() for r in self.store.relations_among(ids, include_structural=False)
            if r.type.value in ("later_version", "possible_contradiction", "references")
        ]
        docs: dict[int, dict] = {}
        for e in engrams:
            p = e.provenance
            d = docs.setdefault(p.document_id, {"document_id": p.document_id, "title": p.document_title,
                                                "fragments": 0, "dates": set()})
            d["fragments"] += 1
            if e.effective_date:
                d["dates"].add(e.effective_date)
        for d in docs.values():
            ds = sorted(d.pop("dates"))
            d["first_date"], d["last_date"] = (ds[0], ds[-1]) if ds else (None, None)
        first = dated[0] if dated else None
        return {
            "topic": topic,
            "first_appearance": {"date": first[0], "date_source": first[1], "engram": first[2].model_dump()} if first else None,
            "latest": {"date": dated[-1][0], "date_source": dated[-1][1], "engram": dated[-1][2].model_dump()} if dated else None,
            "chronology": [{"date": d, "date_source": s, "engram": e.model_dump()} for d, s, e in dated],
            "undated": [e.model_dump() for e in undated],
            "decisions": [{"date": d, "engram": e.model_dump()} for d, _, e in dated if e.kind.value == "decision"]
                         + [{"date": None, "engram": e.model_dump()} for e in undated if e.kind.value == "decision"],
            "changes": rels,
            "documents": sorted(docs.values(), key=lambda d: (d["first_date"] is None, d["first_date"] or "")),
            "notes": (["No dated records match this topic; its chronology cannot be determined."] if not dated and engrams else [])
                     + ([f"{len(undated)} relevant fragment(s) have no recorded date."] if undated else [])
                     + (["Nothing in memory matches this topic."] if not engrams else []),
        }
