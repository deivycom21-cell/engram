"""Associative memory: proposes typed, explained relations between engrams of *different* documents.

Semantic similarity is **not** a factual relation. It is stored as its own type
(``semantically_similar``) with the score and the provider that produced it, and it is
always a *proposal* (status ``proposed``) the user can confirm or reject. Stronger
typed relations (duplicate, contradiction, later version) require additional evidence
beyond similarity (lexical overlap, opposite polarity, dates).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from engram.core.textutil import content_words, norm
from engram.intelligence import contradictions
from engram.memory.models import Engram, RelationOrigin, RelationType
from engram.memory.store import MemoryStore
from engram.memory.vectors import VectorIndex

log = logging.getLogger("engram.associations")

EVOLVING_KINDS = {"decision", "idea", "project", "concept", "question"}


@dataclass
class AssociationReport:
    similar: int = 0
    shared_concepts: int = 0
    duplicates: int = 0
    contradictions: int = 0
    evolutions: int = 0
    examined: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def jaccard(a: str, b: str) -> float:
    sa, sb = set(content_words(a)), set(content_words(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class AssociationEngine:
    def __init__(self, store: MemoryStore, vectors: VectorIndex | None, provider, settings):
        self.store = store
        self.vectors = vectors
        self.provider = provider
        self.top_k = settings.association_top_k
        th = provider.thresholds if provider else None
        self.t_assoc = settings.association_threshold or (th.association if th else 1.1)
        self.t_dup = settings.duplicate_threshold or (th.duplicate if th else 1.1)
        self.t_topic = th.topic if th else 1.1
        self.sim_label = (
            f"neural embedding similarity ({provider.name})" if provider and provider.neural
            else f"lexical vector similarity ({provider.name}, non-neural)" if provider else "n/a"
        )

    # ------------------------------------------------------------------ public
    def associate(self, engram_ids: list[int]) -> AssociationReport:
        rep = AssociationReport()
        if not engram_ids:
            return rep
        engrams = {e.id: e for e in self.store.get_engrams(engram_ids)}
        doc_count = max(1, self.store.db.query_one("SELECT count(*) FROM documents WHERE status='active'")[0])
        df = self.store.concept_document_frequency()
        for eid in engram_ids:
            e = engrams.get(eid)
            if not e or e.status != "active":
                continue
            rep.examined += 1
            topic_partners: dict[int, dict] = {}
            self._exact_duplicates(e, rep)
            if self.vectors is not None:
                self._vector_neighbours(e, rep, topic_partners)
            self._concept_overlap(e, rep, topic_partners, df, doc_count)
            if topic_partners:
                # Bound the pairwise checks: keep the strongest topical partners only.
                ranked = sorted(topic_partners.items(),
                                key=lambda kv: -(kv[1].get("similarity") or 0) - 0.05 * len(kv[1].get("shared") or []))
                topic_partners = dict(ranked[:12])
                partners = {p.id: p for p in self.store.get_engrams(list(topic_partners))}
                evolution: list[tuple] = []
                for pid, info in topic_partners.items():
                    other = partners.get(pid)
                    if other is None or other.status != "active":
                        continue
                    shared = self._shared_concepts(e, other)
                    self._contradiction(e, other, shared, info, rep)
                    cand = self._evolution_candidate(e, other, shared, info)
                    if cand:
                        evolution.append(cand)
                # Only the closest earlier/later counterpart becomes a "later_version" proposal.
                for direction in ("newer", "older"):
                    best = max((c for c in evolution if c[1] == direction), default=None, key=lambda c: c[0])
                    if best:
                        rep.evolutions += self._add_evolution(*best[2:])
        return rep

    # ------------------------------------------------------------------ detectors
    def _exact_duplicates(self, e: Engram, rep: AssociationReport) -> None:
        others = [o for o in self.store.engram_ids_by_hash(e.content_hash) if o != e.id]
        lineage = self._lineage(others)
        mine = self._lineage([e.id]).get(e.id)
        for other_id in others:
            if lineage.get(other_id) == mine:
                continue  # unchanged text carried over between versions of the same file
            if self.store.add_relation(
                e.id, other_id, RelationType.POSSIBLE_DUPLICATE, RelationOrigin.INFERRED,
                "Identical text (same normalised content hash) appears in two places.",
                1.0, "content-hash", {"content_hash": e.content_hash},
            ):
                rep.duplicates += 1

    def _vector_neighbours(self, e: Engram, rep: AssociationReport, topic: dict) -> None:
        vec = self.vectors.get(e.id)
        if vec is None:
            return
        neighbours = self.vectors.knn(vec, k=self.top_k * 4 + 5, exclude={e.id})
        if not neighbours:
            return
        doc_of = self._doc_map([n for n, _ in neighbours])
        lineage = self._lineage([n for n, _ in neighbours])
        mine = self._lineage([e.id]).get(e.id)
        added = dups = 0
        for other_id, sim in neighbours:
            doc, pos = doc_of.get(other_id, (None, None))
            if doc == e.provenance.document_id:
                # Same document: no similarity link (structure already connects them), but distant
                # passages on the same topic are checked for contradictions / evolution.
                if sim >= self.t_topic and pos is not None and abs(pos - (e.provenance.position or 0)) >= 2:
                    topic.setdefault(other_id, {})["similarity"] = sim
                continue
            if sim >= self.t_topic:
                topic.setdefault(other_id, {})["similarity"] = sim
            if lineage.get(other_id) == mine:
                continue  # another version of the same file: only evolution/contradiction checks apply
            if sim >= self.t_dup and dups < self.top_k:
                dups += 1
                other_text = self._content(other_id)
                jac = jaccard(e.content, other_text)
                if jac >= 0.5:
                    if self.store.add_relation(
                        e.id, other_id, RelationType.POSSIBLE_DUPLICATE, RelationOrigin.INFERRED,
                        f"Near-identical content: {self.sim_label} {sim:.3f} and {jac:.0%} word overlap.",
                        min(0.99, sim), "vector-knn+jaccard", {"similarity": sim, "jaccard": jac},
                    ):
                        rep.duplicates += 1
                    continue
            if sim >= self.t_assoc and added < self.top_k:
                added += 1
                if self.store.add_relation(
                    e.id, other_id, RelationType.SEMANTICALLY_SIMILAR, RelationOrigin.INFERRED,
                    f"Similar meaning, not a documented link: {self.sim_label} = {sim:.3f} "
                    f"(threshold {self.t_assoc:.2f}).",
                    sim, "vector-knn", {"similarity": round(sim, 4), "provider": self.provider.name},
                ):
                    rep.similar += 1

    def _concept_overlap(self, e: Engram, rep: AssociationReport, topic: dict, df: dict[int, int], n_docs: int) -> None:
        if not e.concepts:
            return
        rows = self.store.db.query(
            "SELECT ec2.engram_id AS other, c.id AS cid, c.name AS name, c.kind AS kind "
            "FROM engram_concepts ec JOIN engram_concepts ec2 ON ec2.concept_id=ec.concept_id AND ec2.engram_id!=ec.engram_id "
            "JOIN engrams g2 ON g2.id=ec2.engram_id JOIN concepts c ON c.id=ec.concept_id "
            "WHERE ec.engram_id=? AND g2.status='active' AND (g2.document_id IS NOT ? AND g2.document_id != ?)",
            (e.id, e.provenance.document_id, e.provenance.document_id or -1),
        )
        by_other: dict[int, dict[str, float]] = {}
        for r in rows:
            d = df.get(r["cid"], 1)
            # Concepts present in most documents carry little information.
            if n_docs >= 4 and d / n_docs > 0.6:
                continue
            idf = math.log(1 + n_docs / d)
            by_other.setdefault(r["other"], {})[r["name"]] = idf * (1.2 if r["kind"] == "entity" else 1.0)
        scored = []
        for other, shared in by_other.items():
            keys = {norm(k) for k in shared}
            if len(keys) < 2:
                continue
            scored.append((sum(shared.values()), other, sorted(shared, key=lambda k: -shared[k])))
        scored.sort(reverse=True)
        lineage = self._lineage([o for _, o, _ in scored])
        mine = self._lineage([e.id]).get(e.id)
        for score, other, names in scored[: self.top_k]:
            topic.setdefault(other, {})["shared"] = names
            if lineage.get(other) == mine:
                continue
            conf = min(0.9, 0.3 + 0.1 * score)
            if self.store.add_relation(
                e.id, other, RelationType.SHARES_CONCEPTS, RelationOrigin.INFERRED,
                f"Both fragments mention: {', '.join(names[:6])} (rarer terms weigh more; non-neural).",
                conf, "concept-overlap", {"shared": names[:12], "score": round(score, 3)},
            ):
                rep.shared_concepts += 1

    def _contradiction(self, a: Engram, b: Engram, shared: list[str], info: dict, rep: AssociationReport) -> None:
        if not shared:
            return
        signals = contradictions.detect(a.content, b.content, shared)
        if not signals:
            return
        s = signals[0]
        da, db_ = a.effective_date, b.effective_date
        timing = ""
        if da and db_ and da != db_:
            newer = "second" if db_ > da else "first"
            timing = f" Dates differ ({da} vs {db_}); the {newer} fragment is more recent, so this may be a change of mind rather than an error."
        if self.store.add_relation(
            a.id, b.id, RelationType.POSSIBLE_CONTRADICTION, RelationOrigin.INFERRED,
            f"{s.explanation} A: “{s.sentence_a[:220]}” — B: “{s.sentence_b[:220]}”.{timing}",
            s.confidence, f"heuristic-contradiction:{s.kind}",
            {"signal": s.kind, "concept": s.concept, "sentence_a": s.sentence_a, "sentence_b": s.sentence_b,
             "date_a": da, "date_b": db_, "all_signals": [x.kind for x in signals]},
        ):
            rep.contradictions += 1

    def _evolution_candidate(self, a: Engram, b: Engram, shared: list[str], info: dict) -> tuple | None:
        """A pair is a candidate 'later version' when it is the same topic (similarity *and* a shared
        concept), both are evolving kinds of knowledge, and their recorded dates differ by a month+."""
        da, db_ = a.effective_date, b.effective_date
        if not da or not db_ or da[:7] == db_[:7]:
            return None
        sim = info.get("similarity")
        if not shared or sim is None or sim < self.t_assoc:
            return None
        old, new = (a, b) if da < db_ else (b, a)
        ko, kn = old.kind.value, new.kind.value
        if ko not in EVOLVING_KINDS or kn not in EVOLVING_KINDS:
            return None
        if not (ko == kn or {ko, kn} <= {"idea", "decision", "project"} or ko == "question"):
            return None
        return (sim + 0.05 * len(shared), "newer" if new.id == a.id else "older", old, new, shared, sim)

    def _add_evolution(self, old: Engram, new: Engram, shared: list[str], sim: float) -> int:
        basis = []
        if shared:
            basis.append(f"shared concepts: {', '.join(shared[:5])}")
        if sim is not None:
            basis.append(f"{self.sim_label} {sim:.3f}")
        return 1 if self.store.add_relation(
            new.id, old.id, RelationType.LATER_VERSION, RelationOrigin.INFERRED,
            f"Same topic revisited later ({'; '.join(basis)}). This {new.kind.value} is dated {new.effective_date}, "
            f"the earlier {old.kind.value} {old.effective_date}. It may show how the thinking evolved.",
            min(0.85, (sim or 0.5) * 0.8 + 0.1 * len(shared[:3])), "temporal-topic",
            {"older": old.id, "newer": new.id, "older_date": old.effective_date, "newer_date": new.effective_date,
             "shared": shared[:8], "similarity": sim},
        ) else 0

    # ------------------------------------------------------------------ helpers
    def _shared_concepts(self, a: Engram, b: Engram) -> list[str]:
        kb = {c["id"] for c in b.concepts}
        return list(dict.fromkeys(c["name"] for c in a.concepts if c["id"] in kb))

    def _doc_map(self, ids: list[int]) -> dict[int, tuple]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["id"]: (r["document_id"], r["position"]) for r in self.store.db.query(
            f"SELECT id, document_id, position FROM engrams WHERE id IN ({marks})", ids)}

    def _lineage(self, ids: list[int]) -> dict[int, str]:
        """Engram id -> filename of its document. Versions of one file share a lineage."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["id"]: r["filename"] for r in self.store.db.query(
            f"SELECT g.id, d.filename FROM engrams g JOIN documents d ON d.id=g.document_id WHERE g.id IN ({marks})", ids)}

    def _content(self, engram_id: int) -> str:
        row = self.store.db.query_one("SELECT content FROM engrams WHERE id=?", (engram_id,))
        return row["content"] if row else ""
