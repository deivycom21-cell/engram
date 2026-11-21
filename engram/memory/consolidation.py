"""Memory consolidation: a periodic, non-destructive pass over the whole memory.

It (re)detects duplicates, related concepts, fragmented memories, possible
contradictions and new relations — and only ever *adds proposals*. It never deletes,
merges or rewrites an engram. Everything it does is written to the audit log.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("engram.consolidation")


class Consolidator:
    def __init__(self, store, associations, insights, pipeline):
        self.store = store
        self.associations = associations
        self.insights = insights
        self.pipeline = pipeline

    def run(self, batch: int = 200) -> dict:
        t0 = time.perf_counter()
        ids = [r["id"] for r in self.store.db.query("SELECT id FROM engrams WHERE status='active' ORDER BY id")]
        totals: dict[str, int] = {}
        if self.associations is not None:
            for i in range(0, len(ids), batch):
                rep = self.associations.associate(ids[i : i + batch]).as_dict()
                for k, v in rep.items():
                    if isinstance(v, int):
                        totals[k] = totals.get(k, 0) + v
        links = self.pipeline.resolve_links() if self.pipeline else 0
        insight_counts = self.insights.generate()
        report = {
            "engrams_examined": len(ids),
            "new_relations": {k: v for k, v in totals.items() if k != "examined"},
            "explicit_links_resolved": links,
            "new_insights": insight_counts,
            "seconds": round(time.perf_counter() - t0, 2),
            "destructive_actions": 0,
        }
        self.store.audit("consolidation.run", report)
        return report
