"""Insights: human-reviewable findings derived from the graph (never applied automatically).

Kinds:
* ``bridge``        — three documents linked through one fragment (A ↔ B ↔ C), with each link explained.
* ``contradiction`` — a proposed ``possible_contradiction`` relation.
* ``duplicate``     — a proposed ``possible_duplicate`` relation.
* ``forgotten``     — a recent memory connects to an old one that was never revisited.
* ``theme``         — a concept that recurs across several documents (with its time span).
* ``fragmented``    — several short fragments in different documents about one topic.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from engram.memory.store import MemoryStore

CROSS_TYPES = ("semantically_similar", "shares_concepts", "references", "later_version")


def _fp(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if len(ts) == 7:
            ts += "-01"
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class InsightEngine:
    def __init__(self, store: MemoryStore):
        self.store = store

    def generate(self) -> dict:
        counts = {k: 0 for k in ("bridge", "contradiction", "duplicate", "forgotten", "theme", "fragmented")}
        db = self.store.db
        info = {r["id"]: dict(r) for r in db.query(
            "SELECT g.id, g.document_id, g.kind, substr(g.content,1,140) AS preview, length(g.content) AS len, "
            "coalesce(g.occurred_at, g.created_at) AS date, g.ingested_at, g.access_count, g.last_accessed_at, "
            "d.title AS doc FROM engrams g JOIN documents d ON d.id=g.document_id WHERE g.status='active'")}
        rels = [r for r in self.store.list_relations(limit=100000) if r.status.value != "rejected"]

        for r in rels:
            if r.type.value == "possible_contradiction" and r.status.value == "proposed":
                a, b = info.get(r.source_id), info.get(r.target_id)
                if a and b:
                    counts["contradiction"] += self.store.upsert_insight(
                        "contradiction", (f"Possible contradiction within “{a['doc']}”" if a["document_id"] == b["document_id"]
                                          else f"Possible contradiction: “{a['doc']}” vs “{b['doc']}”"), r.explanation,
                        [r.source_id, r.target_id], 0.6 + r.confidence * 0.4, _fp("contradiction", r.id))
            elif r.type.value == "possible_duplicate" and r.status.value == "proposed":
                a, b = info.get(r.source_id), info.get(r.target_id)
                if a and b:
                    counts["duplicate"] += self.store.upsert_insight(
                        "duplicate", f"Possible duplicate in “{a['doc']}” and “{b['doc']}”",
                        r.explanation + " Nothing will be merged or deleted automatically.",
                        [r.source_id, r.target_id], 0.3 + r.confidence * 0.2, _fp("duplicate", r.id))

        # ---- bridges (triads across three documents)
        adj: dict[int, list] = {}
        for r in rels:
            if r.type.value not in CROSS_TYPES:
                continue
            a, b = info.get(r.source_id), info.get(r.target_id)
            if not a or not b or a["document_id"] == b["document_id"]:
                continue
            adj.setdefault(r.source_id, []).append((r.target_id, r))
            adj.setdefault(r.target_id, []).append((r.source_id, r))
        best: dict[tuple, tuple] = {}
        for mid, links in adj.items():
            m = info[mid]
            for i in range(len(links)):
                for j in range(i + 1, len(links)):
                    (x, rx), (y, ry) = links[i], links[j]
                    dx, dy = info[x]["document_id"], info[y]["document_id"]
                    if len({dx, dy, m["document_id"]}) < 3:
                        continue
                    score = min(rx.confidence, ry.confidence)
                    key = tuple(sorted((dx, dy, m["document_id"])))
                    if key not in best or score > best[key][0]:
                        best[key] = (score, x, mid, y, rx, ry)
        for key, (score, x, mid, y, rx, ry) in sorted(best.items(), key=lambda kv: -kv[1][0])[:30]:
            a, m, c = info[x], info[mid], info[y]
            expl = (f"“{m['doc']}” ({m['kind']}) connects “{a['doc']}” ({a['kind']}) and “{c['doc']}” ({c['kind']}). "
                    f"Link 1 [{rx.type.value}, {rx.origin.value}]: {rx.explanation} "
                    f"Link 2 [{ry.type.value}, {ry.origin.value}]: {ry.explanation}")
            counts["bridge"] += self.store.upsert_insight(
                "bridge", f"Connection across 3 documents: {a['doc']} · {m['doc']} · {c['doc']}", expl,
                [x, mid, y], 0.5 + score * 0.5, _fp("bridge", *key))

        # ---- forgotten connections
        now = datetime.now(timezone.utc)
        recent_cut = now - timedelta(days=30)
        for r in rels:
            if r.origin.value != "inferred" or r.type.value not in CROSS_TYPES:
                continue
            a, b = info.get(r.source_id), info.get(r.target_id)
            if not a or not b:
                continue
            for new, old in ((a, b), (b, a)):
                new_t = _parse(new["last_accessed_at"]) or _parse(new["ingested_at"])
                old_t = _parse(old["date"]) or _parse(old["ingested_at"])
                new_d = _parse(new["date"]) or new_t
                if not new_t or not old_t or new_t < recent_cut or old["access_count"] > 0:
                    continue
                if new_d and (new_d - old_t).days >= 180:
                    counts["forgotten"] += self.store.upsert_insight(
                        "forgotten", f"Older memory related to recent work: “{old['doc']}”",
                        f"A recent fragment in “{new['doc']}” relates to an older one ({old['date'] or 'ingested ' + old['ingested_at'][:10]}) "
                        f"that has never been opened. {r.explanation}",
                        [new["id"], old["id"]], 0.55 + r.confidence * 0.3, _fp("forgotten", r.id))
                    break

        # ---- recurring themes
        for row in db.query(
            "SELECT c.id, c.name, c.kind, count(DISTINCT g.document_id) AS docs, count(DISTINCT g.id) AS n, "
            "min(coalesce(g.occurred_at, g.created_at)) AS first, max(coalesce(g.occurred_at, g.created_at)) AS last, "
            "group_concat(DISTINCT g.id) AS ids FROM concepts c JOIN engram_concepts ec ON ec.concept_id=c.id "
            "JOIN engrams g ON g.id=ec.engram_id WHERE g.status='active' GROUP BY c.id HAVING docs >= 3 "
            "ORDER BY docs DESC, n DESC LIMIT 25"
        ):
            span = f" from {row['first']} to {row['last']}" if row["first"] else " (no dates recorded)"
            ids = [int(i) for i in str(row["ids"]).split(",")][:12]
            counts["theme"] += self.store.upsert_insight(
                "theme", f"Recurring theme: {row['name']}",
                f"“{row['name']}” ({row['kind']}) appears in {row['docs']} documents and {row['n']} fragments{span}.",
                ids, 0.3 + min(0.5, row["docs"] * 0.05), _fp("theme", row["id"]))

        # ---- fragmented memories: short fragments in ≥2 docs, densely related
        short = {i for i, v in info.items() if v["len"] < 300}
        groups: dict[int, set[int]] = {}
        for r in rels:
            if r.type.value in ("semantically_similar", "shares_concepts") and r.source_id in short and r.target_id in short:
                groups.setdefault(r.source_id, {r.source_id}).add(r.target_id)
                groups.setdefault(r.target_id, {r.target_id}).add(r.source_id)
        emitted: set[frozenset] = set()
        for center, members in groups.items():
            docs = {info[m]["document_id"] for m in members}
            key = frozenset(members)
            if len(members) >= 3 and len(docs) >= 2 and key not in emitted:
                emitted.add(key)
                ids = sorted(members)
                counts["fragmented"] += self.store.upsert_insight(
                    "fragmented", f"Fragmented memory: {len(ids)} short notes on one topic",
                    f"{len(ids)} short fragments across {len(docs)} documents are closely related. "
                    f"They might be worth consolidating into one note (ENGRAM will not do it automatically).",
                    ids, 0.35, _fp("fragmented", *ids))
        return counts
