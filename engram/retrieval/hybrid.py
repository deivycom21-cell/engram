"""Hybrid contextual recall: full-text + vector + graph spreading + temporal/type context."""
from __future__ import annotations

from engram.memory.models import Engram, RelationStatus
from engram.memory.store import MemoryStore
from engram.retrieval.query import ParsedQuery, parse_query
from engram.retrieval.ranking import RELATION_WEIGHT, Candidate, contextual_adjust, rrf_fuse
from engram.retrieval.search import fts_search, vector_search


class HybridRetriever:
    def __init__(self, store: MemoryStore, vectors, provider):
        self.store = store
        self.vectors = vectors
        self.provider = provider
        if provider is not None:
            # A vector-only hit must be close to "same topic" to be shown at all.
            self.vec_floor = provider.thresholds.topic - 0.12
            self.sim_label = "semantic similarity" if provider.neural else "lexical-vector similarity"
        else:
            self.vec_floor, self.sim_label = 1.1, "similarity"

    def search(self, query: str, limit: int = 20, kinds: list[str] | None = None, expand: bool = True,
               document_id: int | None = None) -> dict:
        pq = parse_query(query)
        fts = fts_search(self.store.db, pq, limit=60)
        vec = vector_search(self.vectors, self.provider, query, limit=60) if query.strip() else []
        if vec:
            # relative floor too: keep vector hits that are competitive with the best one
            best = vec[0][1]
            floor = max(self.vec_floor, best - 0.15)
        else:
            floor = self.vec_floor
        cands = rrf_fuse(fts, vec, floor)

        if expand and cands:
            self._spread(cands)

        engrams = {e.id: e for e in self.store.get_engrams(list(cands))}
        cands = {i: c for i, c in cands.items() if i in engrams and engrams[i].status == "active"}
        if kinds:
            cands = {i: c for i, c in cands.items() if engrams[i].kind.value in kinds}
        if document_id is not None:
            cands = {i: c for i, c in cands.items() if engrams[i].provenance.document_id == document_id}
        contextual_adjust(cands, engrams, pq)

        ranked = sorted(cands.values(), key=lambda c: -c.score)[:limit]
        top = ranked[0].score if ranked else 1.0
        results = [self._result(c, engrams[c.engram_id], top, rank) for rank, c in enumerate(ranked)]
        self.store.touch([c.engram_id for c in ranked[:5]])
        return {
            "query": query,
            "analysis": {
                "terms": pq.terms, "fts_expression": pq.fts_query, "kind_hints": pq.kind_hints,
                "prefer": pq.prefer, "date_from": pq.date_from, "date_to": (pq.date_to or "").rstrip("~") or None,
                "asks_why": pq.asks_why,
            },
            "signals": {
                "fulltext_hits": len(fts), "vector_hits": len([v for v in vec if v[1] >= floor]),
                "vector_provider": self.provider.name if self.provider else None,
                "vector_neural": bool(self.provider and self.provider.neural),
                "graph_expanded": sum(1 for c in ranked if c.via and c.fts_rank is None and c.similarity is None),
            },
            "results": results,
        }

    def _spread(self, cands: dict[int, Candidate], seeds: int = 8) -> None:
        """One hop of spreading activation from the strongest direct hits along stored relations."""
        top = sorted(cands.values(), key=lambda c: -c.score)[:seeds]
        for rank, seed in enumerate(top, start=1):
            for rel in self.store.relations_for(seed.engram_id):
                if rel.status == RelationStatus.REJECTED:
                    continue
                other = rel.target_id if rel.source_id == seed.engram_id else rel.source_id
                w = RELATION_WEIGHT.get(rel.type.value, 0.3) * (rel.confidence if rel.origin.value == "inferred" else 1.0)
                bonus = seed.score * w * 0.5
                via = {"from": seed.engram_id, "from_rank": rank, "type": rel.type.value, "origin": rel.origin.value,
                       "relation_id": rel.id}
                if other in cands:
                    cands[other].score += bonus * 0.3
                    if len(cands[other].via) < 3:
                        cands[other].via.append(via)
                else:
                    c = Candidate(other, score=bonus)
                    c.via.append(via)
                    cands[other] = c

    def _result(self, c: Candidate, e: Engram, top: float, rank: int) -> dict:
        return {
            "rank": rank + 1,
            "score": round(c.score, 6),
            "relevance": round(c.score / top, 3) if top else 0,
            "why": c.why(self.sim_label),
            "signals": {"fts_rank": c.fts_rank, "bm25": c.bm25, "vector_rank": c.vector_rank,
                        "similarity": c.similarity, "via": c.via},
            "snippet": c.snippet,
            "engram": e.model_dump(),
        }


__all__ = ["HybridRetriever", "ParsedQuery"]
