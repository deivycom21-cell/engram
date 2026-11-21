"""EngramEngine: the composition root. Wires storage, providers, pipeline, retrieval and intelligence."""
from __future__ import annotations

import json
import logging
import queue
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from engram import __version__
from engram.core.config import Settings, load_settings
from engram.core.db import Database
from engram.core.security import ensure_within
from engram.ingestion.pipeline import IngestionPipeline, IngestionResult
from engram.intelligence import llm
from engram.intelligence.associations import AssociationEngine
from engram.intelligence.insights import InsightEngine
from engram.memory.consolidation import Consolidator
from engram.memory.embeddings import create_provider
from engram.memory.graph import KnowledgeGraph
from engram.memory.models import now_iso
from engram.memory.store import MemoryStore
from engram.memory.temporal import TemporalMemory
from engram.memory.vectors import VectorIndex
from engram.retrieval.hybrid import HybridRetriever
from engram.retrieval.reconstruction import Reconstructor

log = logging.getLogger("engram.engine")


class EngramEngine:
    def __init__(self, settings: Settings | None = None, **overrides):
        self.settings = settings or load_settings(**overrides)
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.provider, self.provider_status = create_provider(self.settings)
        self.vectors = VectorIndex(self.db, self.provider.name, self.provider.dim) if self.provider else None
        self.store = MemoryStore(self.db, self.provider.name if self.provider else None)
        self.graph = KnowledgeGraph(self.store)
        self.temporal = TemporalMemory(self.store)
        self.associations = AssociationEngine(self.store, self.vectors, self.provider, self.settings)
        self.pipeline = IngestionPipeline(self.settings, self.store, self.vectors, self.provider, self.associations)
        self.retriever = HybridRetriever(self.store, self.vectors, self.provider)
        self.reconstructor = Reconstructor(self.store, self.retriever, self.temporal)
        self.insights = InsightEngine(self.store)
        self.consolidator = Consolidator(self.store, self.associations, self.insights, self.pipeline)
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._recover_interrupted_jobs()

    # ------------------------------------------------------------------ ingestion
    def ingest(self, path: str | Path, recursive: bool = True, consolidate: bool = True) -> list[IngestionResult]:
        results = self.pipeline.ingest_path(path, recursive)
        if consolidate and any(r.status == "ingested" for r in results):
            self.insights.generate()
        return results

    def ingest_bytes(self, data: bytes, filename: str) -> IngestionResult:
        res = self.pipeline.ingest_bytes(data, filename)
        if res.status == "ingested":
            self.insights.generate()
        return res

    def submit_upload(self, data: bytes, filename: str) -> int:
        """Queue an upload for background ingestion; returns a job id."""
        job_id = self.store.create_job("ingest", {"filename": filename, "bytes": len(data)})
        self._ensure_worker()
        self._jobs.put((job_id, data, filename))
        return job_id

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._work, name="engram-ingest", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id, data, filename = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self.store.update_job(job_id, "running")
            try:
                res = self.ingest_bytes(data, filename)
                self.store.update_job(job_id, "failed" if res.status == "failed" else "done", res.as_dict(), res.error)
            except Exception as exc:  # report, never swallow
                log.exception("job %s failed", job_id)
                self.store.update_job(job_id, "failed", None, f"{exc.__class__.__name__}: {exc}")
            finally:
                self._jobs.task_done()

    def wait_for_jobs(self, timeout: float = 120) -> None:
        done = threading.Event()

        def _join():
            self._jobs.join()
            done.set()

        threading.Thread(target=_join, daemon=True).start()
        done.wait(timeout)

    def _recover_interrupted_jobs(self) -> None:
        self.db.execute(
            "UPDATE jobs SET status='failed', error='interrupted by shutdown; please re-upload', updated_at=? "
            "WHERE status IN ('queued','running')", (now_iso(),))

    # ------------------------------------------------------------------ recall
    def search(self, query: str, limit: int = 20, kinds: list[str] | None = None, expand: bool = True) -> dict:
        return self.retriever.search(query, limit=limit, kinds=kinds, expand=expand)

    def reconstruct(self, question: str, narrate: bool = False, consent_external: bool = False) -> dict:
        rec = self.reconstructor.reconstruct(question)
        if narrate:
            try:
                rec["narrative"] = llm.narrate(self.settings, rec, consent_external)
            except llm.LLMUnavailable as exc:
                rec["narrative"] = {"error": str(exc)}
        self.store.audit("reconstruction", {"question": question[:200], "evidence": len(rec["evidence"]),
                                            "narrated": bool(narrate and "text" in rec.get("narrative", {}))})
        return rec

    def evolution(self, topic: str) -> dict:
        return self.temporal.evolution(topic, self.retriever)

    def consolidate(self) -> dict:
        return self.consolidator.run()

    def engram_detail(self, engram_id: int) -> dict | None:
        e = self.store.get_engram(engram_id)
        if not e:
            return None
        rels = self.store.relations_for(engram_id, include_rejected=True)
        other_ids = {r.target_id if r.source_id == engram_id else r.source_id for r in rels}
        others = {o.id: o for o in self.store.get_engrams(list(other_ids), with_concepts=False)}
        relations = []
        for r in rels:
            oid = r.target_id if r.source_id == engram_id else r.source_id
            o = others.get(oid)
            relations.append({
                **r.model_dump(), "direction": "out" if r.source_id == engram_id else "in",
                "other": {"id": oid, "kind": o.kind.value if o else None,
                          "document": o.provenance.document_title if o else None,
                          "date": o.effective_date if o else None,
                          "preview": (o.content[:200] if o else None)},
            })
        doc = self.store.get_document(e.provenance.document_id) if e.provenance.document_id else None
        versions = []
        if doc:
            versions = [{"id": d.id, "ingested_at": d.ingested_at, "authored_at": d.authored_at, "sha256": d.sha256[:12]}
                        for d in self.store.list_documents() if d.filename == doc.filename]
        self.store.touch([engram_id])
        return {"engram": e.model_dump(), "relations": relations, "document": doc.model_dump() if doc else None,
                "document_versions": versions}

    def original_file(self, document_id: int) -> tuple[Path, str] | None:
        doc = self.store.get_document(document_id)
        if not doc:
            return None
        path = ensure_within(self.settings.sources_dir, self.settings.sources_dir / doc.stored_path)
        return (path, doc.filename) if path.is_file() else None

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "version": __version__,
            "data_dir": str(self.settings.data_dir),
            "schema_version": self.db.schema_version,
            "embedding_provider": self.provider.name if self.provider else None,
            "embedding_neural": bool(self.provider and self.provider.neural),
            "embedding_status": self.provider_status,
            "vector_backend": ("sqlite-vec" if self.db.vec_available else "numpy") if self.vectors else None,
            "llm": llm.available(self.settings),
            "privacy": {
                "network_listen": f"{self.settings.host}:{self.settings.port}",
                "external_services_enabled": bool(self.settings.llm_provider == "claude-cli" and self.settings.allow_external_llm),
            },
        }

    def overview(self) -> dict:
        return {
            "status": self.status(),
            "stats": self.store.stats(),
            "documents": [d.model_dump() for d in self.store.list_documents()[:50]],
            "top_concepts": self.store.top_concepts(24),
            "activity": self.store.recent_activity(25),
            "jobs": self.store.recent_jobs(10),
        }

    # ------------------------------------------------------------------ backup / export
    def backup(self) -> Path:
        """Zip of a consistent DB snapshot + original sources + config. Nothing is removed."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.settings.backups_dir / f"engram-backup-{ts}.zip"
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "engram.db"
            self.db.backup_to(snap)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "engram.db")
                for f in self.settings.sources_dir.rglob("*"):
                    if f.is_file() and not f.name.endswith(".part"):
                        z.write(f, f"sources/{f.relative_to(self.settings.sources_dir).as_posix()}")
                cfg = self.settings.data_dir / "engram.toml"
                if cfg.is_file():
                    z.write(cfg, "engram.toml")
                z.writestr("MANIFEST.json", json.dumps({"engram_version": __version__, "created": now_iso(),
                                                        "schema_version": self.db.schema_version}, indent=2))
        self.store.audit("backup.created", {"path": dest.name})
        return dest

    def export_json(self) -> Path:
        """Portable, human-readable export (no embeddings: they are derived and can be recomputed)."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.settings.exports_dir / f"engram-export-{ts}.json"
        q = self.db.query
        data = {
            "engram_version": __version__,
            "exported_at": now_iso(),
            "documents": [dict(r) for r in q("SELECT * FROM documents ORDER BY id")],
            "engrams": [dict(r) for r in q("SELECT * FROM engrams ORDER BY id")],
            "concepts": [dict(r) for r in q("SELECT * FROM concepts ORDER BY id")],
            "engram_concepts": [dict(r) for r in q("SELECT * FROM engram_concepts")],
            "relations": [dict(r) for r in q("SELECT * FROM relations ORDER BY id")],
            "insights": [dict(r) for r in q("SELECT * FROM insights ORDER BY id")],
        }
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), "utf-8")
        self.store.audit("export.created", {"path": dest.name})
        return dest

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def disk_free_mb(path: Path) -> int:
    return int(shutil.disk_usage(path).free / (1 << 20))
