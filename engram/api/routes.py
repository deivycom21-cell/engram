"""HTTP API (local only). All handlers are thin: the engine does the work."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from engram.api.schemas import EngramPatch, InsightPatch, ReconstructRequest, RelationPatch
from engram.core.engine import EngramEngine
from engram.core.security import safe_filename

router = APIRouter(prefix="/api")

KINDS = ["fact", "idea", "event", "decision", "concept", "observation", "question", "project"]


def engine(request: Request) -> EngramEngine:
    return request.app.state.engine


def _kinds(kinds: str | None) -> list[str] | None:
    if not kinds:
        return None
    out = [k.strip().lower() for k in kinds.split(",") if k.strip()]
    bad = [k for k in out if k not in KINDS]
    if bad:
        raise HTTPException(422, f"unknown kind(s): {', '.join(bad)}")
    return out


# ------------------------------------------------------------------ status / overview
@router.get("/status")
def status(e: EngramEngine = Depends(engine)):
    return e.status()


@router.get("/overview")
def overview(e: EngramEngine = Depends(engine)):
    return e.overview()


# ------------------------------------------------------------------ ingestion
@router.post("/ingest")
async def ingest(request: Request, files: list[UploadFile] = File(...), e: EngramEngine = Depends(engine)):
    if len(files) > 50:
        raise HTTPException(413, "at most 50 files per request")
    limit = e.settings.max_file_bytes
    jobs = []
    for f in files:
        name = safe_filename(f.filename or "upload")
        data = bytearray()
        while chunk := await f.read(1 << 20):
            data += chunk
            if len(data) > limit:
                raise HTTPException(413, f"{name}: larger than {limit // (1 << 20)} MB")
        jobs.append({"filename": name, "job_id": e.submit_upload(bytes(data), name)})
    return {"jobs": jobs}


@router.get("/jobs/{job_id}")
def job(job_id: int, e: EngramEngine = Depends(engine)):
    j = e.store.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


# ------------------------------------------------------------------ documents
@router.get("/documents")
def documents(e: EngramEngine = Depends(engine)):
    return [d.model_dump() for d in e.store.list_documents()]


@router.get("/documents/{doc_id}")
def document(doc_id: int, e: EngramEngine = Depends(engine)):
    d = e.store.get_document(doc_id)
    if not d:
        raise HTTPException(404, "document not found")
    engrams, _ = e.store.list_engrams(document_id=doc_id, limit=2000, order="position")
    return {"document": d.model_dump(), "engrams": [g.model_dump() for g in engrams]}


@router.get("/documents/{doc_id}/original")
def original(doc_id: int, e: EngramEngine = Depends(engine)):
    found = e.original_file(doc_id)
    if not found:
        raise HTTPException(404, "original not found")
    path, filename = found
    # Always served as a download with a neutral type: imported files are never rendered or executed.
    return FileResponse(path, filename=filename, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------------ engrams
@router.get("/engrams")
def engrams(kinds: str | None = None, document_id: int | None = None, limit: int = Query(100, le=1000),
            offset: int = Query(0, ge=0), order: str = "recent", e: EngramEngine = Depends(engine)):
    items, total = e.store.list_engrams(_kinds(kinds), document_id, limit, offset, order)
    return {"total": total, "items": [g.model_dump() for g in items]}


@router.get("/engrams/{engram_id}")
def engram(engram_id: int, e: EngramEngine = Depends(engine)):
    d = e.engram_detail(engram_id)
    if not d:
        raise HTTPException(404, "engram not found")
    return d


@router.patch("/engrams/{engram_id}")
def patch_engram(engram_id: int, body: EngramPatch, e: EngramEngine = Depends(engine)):
    if not e.store.get_engram(engram_id):
        raise HTTPException(404, "engram not found")
    if body.kind:
        e.store.set_engram_kind(engram_id, body.kind.value)
    if body.status:
        e.store.set_engram_status(engram_id, body.status)
        e.vectors and e.vectors.invalidate()
    return e.engram_detail(engram_id)


# ------------------------------------------------------------------ recall
@router.get("/search")
def search(q: str = Query(..., min_length=1, max_length=1000), limit: int = Query(20, le=100), kinds: str | None = None,
           expand: bool = True, e: EngramEngine = Depends(engine)):
    return e.search(q, limit=limit, kinds=_kinds(kinds), expand=expand)


@router.post("/reconstruct")
def reconstruct(body: ReconstructRequest, e: EngramEngine = Depends(engine)):
    return e.reconstruct(body.question, narrate=body.narrate, consent_external=body.consent_external)


@router.get("/timeline")
def timeline(kinds: str | None = None, date_from: str | None = Query(None, alias="from", max_length=10),
             date_to: str | None = Query(None, alias="to", max_length=10), document_id: int | None = None,
             limit: int = Query(500, le=5000), e: EngramEngine = Depends(engine)):
    return e.temporal.timeline(_kinds(kinds), date_from, date_to, document_id, limit)


@router.get("/evolution")
def evolution(q: str = Query(..., min_length=2, max_length=500), e: EngramEngine = Depends(engine)):
    return e.evolution(q)


# ------------------------------------------------------------------ graph
@router.get("/graph")
def graph(limit: int = Query(600, le=3000), kinds: str | None = None, structural: bool = True, q: str | None = None,
          e: EngramEngine = Depends(engine)):
    seeds = None
    if q:
        seeds = [r["engram"]["id"] for r in e.search(q, limit=25)["results"]]
    return e.graph.subgraph(limit=limit, kinds=_kinds(kinds), include_structural=structural, seed_ids=seeds)


@router.get("/graph/path")
def graph_path(a: int, b: int, e: EngramEngine = Depends(engine)):
    p = e.graph.path(a, b)
    return {"found": p is not None, "steps": p or []}


@router.get("/engrams/{engram_id}/neighbours")
def neighbours(engram_id: int, depth: int = Query(1, ge=1, le=3), e: EngramEngine = Depends(engine)):
    return e.graph.neighbours(engram_id, depth=depth)


@router.get("/concepts")
def concepts(q: str | None = Query(None, max_length=200), limit: int = Query(30, le=200), e: EngramEngine = Depends(engine)):
    return e.store.search_concepts(q, limit) if q else e.store.top_concepts(limit)


@router.get("/concepts/{concept_id}/engrams")
def concept_engrams(concept_id: int, e: EngramEngine = Depends(engine)):
    return [g.model_dump() for g in e.store.get_engrams(e.store.engrams_with_concept(concept_id))]


# ------------------------------------------------------------------ relations & insights
@router.get("/relations")
def relations(status: str | None = None, types: str | None = None, origin: str | None = None,
              limit: int = Query(200, le=2000), e: EngramEngine = Depends(engine)):
    t = [x.strip() for x in types.split(",")] if types else None
    return [r.model_dump() for r in e.store.list_relations(t, status, origin, limit)]


@router.patch("/relations/{rel_id}")
def patch_relation(rel_id: int, body: RelationPatch, e: EngramEngine = Depends(engine)):
    r = e.store.get_relation(rel_id)
    if not r:
        raise HTTPException(404, "relation not found")
    if r.origin.value != "inferred":
        raise HTTPException(409, "only inferred relations can be confirmed or rejected")
    return e.store.set_relation_status(rel_id, body.status).model_dump()


@router.get("/insights")
def insights(status: str | None = "open", kind: str | None = None, e: EngramEngine = Depends(engine)):
    items = e.store.list_insights(status or None, kind)
    ids = sorted({i for it in items for i in it["engram_ids"]})
    previews = {g.id: {"id": g.id, "kind": g.kind.value, "document": g.provenance.document_title,
                       "date": g.effective_date, "preview": g.content[:220]}
                for g in e.store.get_engrams(ids, with_concepts=False)}
    for it in items:
        it["engrams"] = [previews[i] for i in it["engram_ids"] if i in previews]
    return items


@router.patch("/insights/{insight_id}")
def patch_insight(insight_id: int, body: InsightPatch, e: EngramEngine = Depends(engine)):
    e.store.set_insight_status(insight_id, body.status)
    return {"ok": True}


@router.post("/consolidate")
def consolidate(e: EngramEngine = Depends(engine)):
    return e.consolidate()


# ------------------------------------------------------------------ backup / export
@router.post("/backup")
def backup(e: EngramEngine = Depends(engine)):
    path = e.backup()
    return {"path": str(path), "size_bytes": path.stat().st_size}


@router.get("/export")
def export(e: EngramEngine = Depends(engine)):
    path = e.export_json()
    return FileResponse(path, filename=path.name, media_type="application/json")
