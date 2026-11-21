"""Rank fusion and context-aware re-ranking. Every adjustment is recorded so results can explain themselves."""
from __future__ import annotations

from dataclasses import dataclass, field

from engram.memory.models import Engram
from engram.retrieval.query import ParsedQuery

RRF_K = 60

# How much activation spreads along each relation type during graph expansion.
RELATION_WEIGHT = {
    "references": 0.8,
    "later_version": 0.7,
    "possible_contradiction": 0.7,
    "semantically_similar": 0.5,
    "shares_concepts": 0.5,
    "follows": 0.35,
    "possible_duplicate": 0.3,
}


@dataclass
class Candidate:
    engram_id: int
    score: float = 0.0
    fts_rank: int | None = None
    bm25: float | None = None
    vector_rank: int | None = None
    similarity: float | None = None
    snippet: str | None = None
    via: list[dict] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)

    def why(self, sim_label: str) -> list[str]:
        out = []
        if self.fts_rank is not None:
            out.append(f"keyword match (BM25 rank #{self.fts_rank + 1})")
        if self.similarity is not None:
            out.append(f"{sim_label} {self.similarity:.3f} (rank #{(self.vector_rank or 0) + 1})")
        for v in self.via[:3]:
            out.append(f"connected to result #{v['from_rank']} via '{v['type']}' ({v['origin']})")
        out.extend(self.adjustments)
        return out


def rrf_fuse(fts: list[tuple[int, float, str]], vec: list[tuple[int, float]], vec_floor: float) -> dict[int, Candidate]:
    cands: dict[int, Candidate] = {}
    for rank, (eid, score, snip) in enumerate(fts):
        c = cands.setdefault(eid, Candidate(eid))
        c.fts_rank, c.bm25, c.snippet = rank, score, snip
        c.score += 1.0 / (RRF_K + rank)
    for rank, (eid, sim) in enumerate(vec):
        if sim < vec_floor and eid not in cands:
            continue  # weak vector-only hit: not relevant enough to show
        c = cands.setdefault(eid, Candidate(eid))
        c.vector_rank, c.similarity = rank, sim
        c.score += 1.0 / (RRF_K + rank)
    return cands


def contextual_adjust(cands: dict[int, Candidate], engrams: dict[int, Engram], pq: ParsedQuery) -> None:
    dated = sorted({e.effective_date for e in engrams.values() if e.effective_date})
    for eid, c in cands.items():
        e = engrams.get(eid)
        if not e:
            continue
        if pq.kind_hints and e.kind.value in pq.kind_hints:
            c.score *= 1.25
            c.adjustments.append(f"type '{e.kind.value}' matches the question")
        d = e.effective_date
        if pq.date_from:
            if d and pq.date_from <= d <= pq.date_to:
                c.score *= 1.35
                c.adjustments.append(f"date {d} inside requested period")
            elif d:
                c.score *= 0.75
            else:
                c.score *= 0.9
        if pq.prefer and d and len(dated) > 1:
            pos = dated.index(d) / (len(dated) - 1)
            factor = 1 + 0.15 * ((1 - pos) if pq.prefer == "older" else pos)
            c.score *= factor
            if factor > 1.05:
                c.adjustments.append(f"{'earlier' if pq.prefer == 'older' else 'more recent'} record ({d})")
