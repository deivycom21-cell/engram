"""Knowledge graph queries over the ``relations`` table.

This is a conventional graph data structure (adjacency in SQLite + BFS in Python).
It is *not* a neural network; only the embedding model is neural.
"""
from __future__ import annotations

from collections import deque

from engram.memory.store import MemoryStore


class KnowledgeGraph:
    def __init__(self, store: MemoryStore):
        self.store = store

    def neighbours(self, engram_id: int, depth: int = 1, include_structural: bool = True, limit: int = 200) -> dict:
        seen = {engram_id: 0}
        edges: dict[int, dict] = {}
        frontier = deque([engram_id])
        while frontier and len(seen) < limit:
            node = frontier.popleft()
            if seen[node] >= depth:
                continue
            for rel in self.store.relations_for(node):
                if not include_structural and rel.origin.value == "structural":
                    continue
                edges[rel.id] = rel.model_dump()
                other = rel.target_id if rel.source_id == node else rel.source_id
                if other not in seen:
                    seen[other] = seen[node] + 1
                    frontier.append(other)
        return {"center": engram_id, "nodes": seen, "edges": list(edges.values())}

    def path(self, a: int, b: int, max_depth: int = 6) -> list[dict] | None:
        """Shortest explained path between two engrams (BFS over non-rejected relations)."""
        if a == b:
            return []
        prev: dict[int, tuple[int, dict]] = {}
        frontier = deque([(a, 0)])
        seen = {a}
        while frontier:
            node, d = frontier.popleft()
            if d >= max_depth:
                continue
            for rel in self.store.relations_for(node):
                other = rel.target_id if rel.source_id == node else rel.source_id
                if other in seen:
                    continue
                seen.add(other)
                prev[other] = (node, rel.model_dump())
                if other == b:
                    steps = []
                    cur = b
                    while cur != a:
                        p, r = prev[cur]
                        steps.append({"from": p, "to": cur, "relation": r})
                        cur = p
                    return list(reversed(steps))
                frontier.append((other, d + 1))
        return None

    def subgraph(self, limit: int = 600, kinds: list[str] | None = None, include_structural: bool = True,
                 seed_ids: list[int] | None = None) -> dict:
        """Nodes + edges for visualisation. With ``seed_ids``, returns the seeds and their 1-hop neighbourhood."""
        db = self.store.db
        if seed_ids:
            ids = set(seed_ids)
            for sid in seed_ids:
                for rel in self.store.relations_for(sid):
                    ids.add(rel.source_id)
                    ids.add(rel.target_id)
            ids = list(ids)[:limit]
        else:
            where, params = ["g.status='active'"], []
            if kinds:
                where.append(f"g.kind IN ({','.join('?' * len(kinds))})")
                params += kinds
            # Most connected engrams first so that a capped graph keeps its structure.
            rows = db.query(
                f"SELECT g.id, (SELECT count(*) FROM relations r WHERE (r.source_id=g.id OR r.target_id=g.id) "
                f"AND r.status!='rejected' AND r.origin!='structural') AS deg FROM engrams g WHERE {' AND '.join(where)} "
                f"ORDER BY deg DESC, g.id LIMIT ?",
                (*params, limit),
            )
            ids = [r["id"] for r in rows]
        if not ids:
            return {"nodes": [], "edges": [], "documents": []}
        marks = ",".join("?" * len(ids))
        node_rows = db.query(
            f"SELECT g.id, g.kind, g.title, substr(g.content, 1, 160) AS preview, g.document_id, "
            f"coalesce(g.occurred_at, g.created_at) AS date, g.kind_confidence, d.title AS doc_title "
            f"FROM engrams g LEFT JOIN documents d ON d.id=g.document_id WHERE g.id IN ({marks})",
            ids,
        )
        rels = self.store.relations_among(ids, include_structural=include_structural)
        degree: dict[int, int] = {}
        for r in rels:
            if r.origin.value != "structural":
                degree[r.source_id] = degree.get(r.source_id, 0) + 1
                degree[r.target_id] = degree.get(r.target_id, 0) + 1
        concept_rows = db.query(
            f"SELECT ec.engram_id, c.name FROM engram_concepts ec JOIN concepts c ON c.id=ec.concept_id "
            f"WHERE ec.engram_id IN ({marks}) ORDER BY ec.weight DESC",
            ids,
        )
        concepts: dict[int, list[str]] = {}
        for r in concept_rows:
            lst = concepts.setdefault(r["engram_id"], [])
            if len(lst) < 6:
                lst.append(r["name"])
        nodes = [
            {
                "id": r["id"], "kind": r["kind"], "title": r["title"], "preview": r["preview"],
                "document_id": r["document_id"], "document": r["doc_title"], "date": r["date"],
                "degree": degree.get(r["id"], 0), "concepts": concepts.get(r["id"], []),
            }
            for r in node_rows
        ]
        edges = [
            {"id": r.id, "source": r.source_id, "target": r.target_id, "type": r.type.value, "origin": r.origin.value,
             "status": r.status.value, "confidence": r.confidence, "explanation": r.explanation}
            for r in rels
        ]
        docs = sorted({(n["document_id"], n["document"]) for n in nodes if n["document_id"]}, key=lambda x: x[0])
        return {"nodes": nodes, "edges": edges, "documents": [{"id": d, "title": t} for d, t in docs]}
