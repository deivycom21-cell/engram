"""Memory reconstruction: assemble a *verifiable* account of how a piece of thinking came to be.

The reconstruction is deterministic. It never writes new claims: every statement is a
quotation or a count, labelled with an evidence id (``[E3]``) that maps to an exact
fragment (document, page/section, character offsets). What cannot be determined from
the sources is listed explicitly under ``unknowns``.

An optional language-model *narrative* can be requested on top (see
intelligence/llm.py). It is clearly labelled as generated and must cite evidence ids.
"""
from __future__ import annotations

import re

from engram.memory.models import Engram
from engram.memory.temporal import TemporalMemory, date_of

CAUSAL = re.compile(
    r"\b(porque|ya que|debido a|dado que|puesto que|por eso|por lo que|así que|asi que|el motivo|la raz[oó]n|"
    r"con el fin de|para evitar|a causa de|because|since|due to|as a result|so that|therefore|the reason|"
    r"caused by|in order to|to avoid|that's why|por ello|por esta raz[oó]n)\b",
    re.I,
)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _excerpt(text: str, n: int = 240) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


class Reconstructor:
    def __init__(self, store, retriever, temporal: TemporalMemory):
        self.store = store
        self.retriever = retriever
        self.temporal = temporal

    def reconstruct(self, question: str, max_evidence: int = 18, min_relevance: float = 0.22) -> dict:
        res = self.retriever.search(question, limit=max_evidence * 2)
        hits = [r for r in res["results"] if r["relevance"] >= min_relevance][:max_evidence]
        ids = [h["engram"]["id"] for h in hits]
        engrams = {e.id: e for e in self.store.get_engrams(ids)}
        if not engrams:
            return self._empty(question, res)

        dated, undated = self.temporal.ordered(list(engrams.values()))
        # Evidence labels follow chronological order so [E1] is the oldest known record.
        order = [e.id for _, _, e in dated] + [e.id for e in undated]
        label = {eid: f"E{i + 1}" for i, eid in enumerate(order)}
        hit_by_id = {h["engram"]["id"]: h for h in hits}

        evidence = []
        for eid in order:
            e = engrams[eid]
            d, src = date_of(e)
            h = hit_by_id.get(eid, {})
            evidence.append({
                "label": label[eid], "date": d, "date_source": src, "kind": e.kind.value,
                "relevance": h.get("relevance"), "why": h.get("why", []), "engram": e.model_dump(),
            })

        cite = lambda e: f"[{label[e.id]}]"  # noqa: E731

        # 1. original idea = oldest dated record of the topic (or note why it cannot be determined)
        original = None
        strong = [t for t in dated if (hit_by_id.get(t[2].id, {}).get("relevance") or 0) >= 0.5]
        if dated:
            d, src, e = (strong or dated)[0]
            original = {"label": label[e.id], "date": d, "date_source": src, "kind": e.kind.value,
                        "excerpt": _excerpt(e.content), "document": e.provenance.document_title}

        # 2. documents involved
        docs: dict[int, dict] = {}
        for e in engrams.values():
            p = e.provenance
            entry = docs.setdefault(p.document_id, {"document_id": p.document_id, "title": p.document_title,
                                                    "filename": p.filename, "labels": [], "dates": []})
            entry["labels"].append(label[e.id])
            if e.effective_date:
                entry["dates"].append(e.effective_date)
        for entry in docs.values():
            entry["labels"].sort(key=lambda s: int(s[1:]))
            ds = sorted(entry.pop("dates"))
            entry["date_range"] = [ds[0], ds[-1]] if ds else None

        # 3. decisions
        decisions = [
            {"label": label[e.id], "date": date_of(e)[0], "excerpt": _excerpt(e.content), "kind_reason": e.kind_reason}
            for e in (x for x in (engrams[i] for i in order) if x.kind.value == "decision")
        ]

        # 4. changes of opinion: inferred contradictions / later versions among the evidence
        changes = []
        for rel in self.store.relations_among(list(engrams), include_structural=False):
            if rel.type.value not in ("possible_contradiction", "later_version"):
                continue
            a, b = engrams[rel.source_id], engrams[rel.target_id]
            if rel.type.value == "later_version":
                a, b = b, a  # stored newer -> older; present older -> newer
            changes.append({
                "type": rel.type.value, "from": label[a.id], "to": label[b.id],
                "from_date": a.effective_date, "to_date": b.effective_date,
                "explanation": rel.explanation, "status": rel.status.value, "relation_id": rel.id,
            })
        changes.sort(key=lambda c: (c["from_date"] or "~", c["to_date"] or "~"))

        # 5. chronology
        chronology = [
            {"label": label[e.id], "date": d, "date_source": src, "kind": e.kind.value,
             "document": e.provenance.document_title, "excerpt": _excerpt(e.content, 160)}
            for d, src, e in dated
        ]

        # 6. stated reasons: sentences with explicit causal language, quoted verbatim
        reasons = []
        for eid in order:
            for sent in _SENT.split(engrams[eid].content):
                if CAUSAL.search(sent) and len(sent.strip()) > 15:
                    reasons.append({"label": label[eid], "sentence": " ".join(sent.split())[:400]})
        reasons = reasons[:8]

        # 7. what cannot be determined
        unknowns = self._unknowns(res, hits, engrams, dated, undated, decisions, reasons, changes, docs)

        confidence = self._confidence(res, hits)
        summary = self._summary(engrams, dated, decisions, reasons, changes, docs, cite, confidence)
        if original and original["label"] != label[dated[0][2].id]:
            summary.insert(2, f"Earliest strongly relevant record [{original['label']}] ({original['date']}, "
                              f"{original['kind']}): “{_excerpt(original['excerpt'], 160)}”")
        return {
            "question": question,
            "retrieval_confidence": confidence,
            "summary": summary,
            "original_idea": original,
            "documents": sorted(docs.values(), key=lambda d: int(d["labels"][0][1:])),
            "decisions": decisions,
            "changes": changes,
            "reasons": reasons,
            "chronology": chronology,
            "undated": [{"label": label[e.id], "kind": e.kind.value, "document": e.provenance.document_title,
                         "excerpt": _excerpt(e.content, 160)} for e in undated],
            "evidence": evidence,
            "unknowns": unknowns,
            "analysis": res["analysis"],
            "method": "Deterministic: hybrid retrieval → chronological ordering → relation lookup → quotation. "
                      "No text in this reconstruction is generated; every line quotes or counts evidence.",
        }

    # ------------------------------------------------------------------ helpers
    def _confidence(self, res: dict, hits: list[dict]) -> dict:
        if not hits:
            return {"level": "none", "reason": "no relevant fragments"}
        top = hits[0]["signals"]
        both = top["fts_rank"] is not None and top["similarity"] is not None
        direct = sum(1 for h in hits if h["signals"]["fts_rank"] is not None or h["signals"]["similarity"] is not None)
        if both and direct >= 3:
            return {"level": "high", "reason": "keyword and semantic signals agree on several fragments"}
        if direct >= 2:
            return {"level": "medium", "reason": "several direct matches, signals partly agree"}
        return {"level": "low", "reason": "few or weak matches; the memory may not contain this"}

    def _unknowns(self, res, hits, engrams, dated, undated, decisions, reasons, changes, docs) -> list[str]:
        out = []
        if not dated:
            out.append("None of the relevant fragments carries a date: the original moment and the order of events cannot be determined.")
        elif undated:
            out.append(f"{len(undated)} relevant fragment(s) have no date, so their place in the chronology is unknown.")
        if not decisions:
            out.append("No explicit decision record was found on this topic.")
        if res["analysis"].get("asks_why") and not reasons:
            out.append("No source states a reason explicitly (no causal sentence such as 'porque…', 'because…').")
        if len(docs) == 1:
            out.append("Only one document covers this topic; it cannot be corroborated by another source.")
        unresolved = [c for c in changes if c["type"] == "possible_contradiction" and c["status"] != "rejected"]
        if unresolved:
            out.append(f"{len(unresolved)} possible contradiction(s) are unresolved; ENGRAM cannot tell which statement is "
                       f"current beyond what the dates show.")
        if any(e.provenance.filename and e.provenance.filename.lower().endswith(".pdf") for e in engrams.values()):
            out.append("Some evidence comes from PDF text extraction; layout-dependent content (tables, figures) may be missing.")
        out.append("Anything not written down in the imported sources (conversations, unrecorded reasons) is outside this memory.")
        return out

    def _summary(self, engrams, dated, decisions, reasons, changes, docs, cite, confidence) -> list[str]:
        lines = []
        span = f", dated {dated[0][0]} → {dated[-1][0]}" if dated else ""
        lines.append(f"{len(engrams)} relevant fragment(s) in {len(docs)} document(s){span}. "
                     f"Retrieval confidence: {confidence['level']}.")
        if dated:
            d, _, e = dated[0]
            lines.append(f"Earliest related record {cite(e)} ({d}, {e.kind.value}): “{_excerpt(e.content, 160)}”")
            if len(dated) > 1:
                d2, _, e2 = dated[-1]
                lines.append(f"Most recent record {cite(e2)} ({d2}, {e2.kind.value}): “{_excerpt(e2.content, 160)}”")
        for dec in decisions[:3]:
            lines.append(f"Decision [{dec['label']}]{' (' + dec['date'] + ')' if dec['date'] else ''}: “{_excerpt(dec['excerpt'], 160)}”")
        for r in reasons[:3]:
            lines.append(f"Stated reason [{r['label']}]: “{_excerpt(r['sentence'], 200)}”")
        for c in changes[:3]:
            word = "possibly contradicts" if c["type"] == "possible_contradiction" else "revisits"
            lines.append(f"[{c['to']}] {word} [{c['from']}] (inferred, {c['status']}).")
        return lines

    def _empty(self, question: str, res: dict) -> dict:
        return {
            "question": question, "retrieval_confidence": {"level": "none", "reason": "no relevant fragments"},
            "summary": ["Nothing in memory matches this question."], "original_idea": None, "documents": [],
            "decisions": [], "changes": [], "reasons": [], "chronology": [], "undated": [], "evidence": [],
            "unknowns": ["The imported sources do not appear to contain information about this."],
            "analysis": res["analysis"], "method": "Deterministic reconstruction (no matches).",
        }


__all__ = ["Reconstructor", "Engram"]
