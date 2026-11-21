# ENGRAM architecture

```
                 ┌──────────────── frontend (vanilla JS + Three.js, served by FastAPI) ────────────────┐
                 │ Overview · Brain explorer (3D) · Search/Reconstruct · Timeline · Inspector · Insights │
                 └───────────────────────────────▲──────────────────────────────────────────────────────┘
                                                 │ JSON over HTTP (127.0.0.1 only)
 engram/api ───── routes.py (thin) ── app.py (CSP, trusted hosts, static files)
                                                 │
 engram/core ──── engine.py  ← composition root: wires everything, job worker, backup/export
                  config.py · db.py (SQLite + migrations) · security.py · textutil.py · lifecycle.py
                                                 │
     ┌──────────────────────────┬────────────────┴──────────────┬──────────────────────────────┐
 ingestion/                 memory/                          retrieval/                   intelligence/
 pipeline.py               store.py (all SQL writes)         query.py   (intent)          associations.py
 markdown.py / pdf.py      vectors.py (sqlite-vec | numpy)   search.py  (FTS5, k-NN)      contradictions.py
 text.py / chunking.py     embeddings.py (ONNX/Ollama/hash)  ranking.py (RRF + context)   insights.py
 metadata.py (dates)       graph.py (BFS, subgraphs)         hybrid.py  (spreading)       llm.py (optional)
 extraction.py             temporal.py · consolidation.py    reconstruction.py
```

## Data model (SQLite, `engram/core/db.py`)

| table | purpose |
|---|---|
| `documents` | one row per imported file version: sha256, stored copy path, title, authored date **and its source**, `previous_version_id` |
| `engrams` | the unit of knowledge: verbatim content, kind (+confidence, reason), section/page, char offsets, `occurred_at`/`created_at` with sources, ingestion time, access stats, status (`active`/`archived`) |
| `engrams_fts` | FTS5 index (external content, kept in sync by triggers) |
| `embeddings` | `(engram_id, model)` → float32 vector. Source of truth for vectors |
| `vec_index` | sqlite-vec `vec0` table for the active model (derived, rebuildable) |
| `concepts`, `engram_concepts` | extracted concepts, entities and tags (explicit vs extracted origin) |
| `relations` | typed edges: `origin` ∈ explicit/structural/inferred, explanation, confidence, evidence JSON, detector, status |
| `insights` | reviewable findings with a stable fingerprint (dismissed ones stay dismissed) |
| `jobs`, `audit_log`, `meta` | background ingestion, transparency log of every automatic action, schema version |

## Ingestion flow

1. **Validate**: extension whitelist, content sniffing (rejects executables/archives in
   disguise, fake PDFs), size limit, symlinks refused, filename sanitised.
2. **Deduplicate** by SHA-256; same name + new bytes → new version.
3. **Store original** content-addressed under `data/sources/ab/<sha256>.<ext>`.
4. **Parse**: Markdown (front-matter, headings, lists, code fences, `#tags`, `[[links]]`),
   PDF (per page via pypdf), text (paragraphs, journal entries).
5. **Chunk** into verbatim spans; **date** each from evidence; **classify** the kind;
   **extract** concepts/entities.
6. **Embed** (batch) and persist in one transaction; add structural `follows` edges.
7. **Resolve explicit links**, then **associate** (duplicates, similarity, shared
   concepts, contradictions, later versions) and refresh **insights**.

## Recall (hybrid)

`score = RRF(BM25 rank, vector rank)` → one hop of **spreading activation** along stored
relations (weights per relation type, inferred edges scaled by confidence) → contextual
adjustments (requested knowledge type, explicit period, "previously/now" intent). Every
result carries `why` (the signals that produced it) and full provenance. Vector-only hits
must clear a similarity floor, so unrelated questions return nothing.

## Reconstruction

Hybrid recall (relevance ≥ 0.22) → chronological ordering by evidence dates →
lookup of contradictions / later versions among the evidence → quotation of decisions and
of sentences with explicit causal language → list of unknowns. Output is JSON with
evidence labels `E1…En` (oldest first) that the UI turns into clickable citations.

## Consolidation

`engram consolidate` (or the button) re-runs association for every engram, resolves links
and regenerates insights. It only inserts proposals (`INSERT OR IGNORE`), never deletes
or edits engrams, and logs a report to `audit_log`.

## Extending

* New file type → a parser returning `ParsedDocument` (see `ingestion/parsed.py`) and a
  branch in `pipeline._ingest`.
* New embedding backend → a class with `name`, `dim`, `neural`, `thresholds`, `embed()`
  in `memory/embeddings.py` and a case in `create_provider`.
* New relation detector → a method in `AssociationEngine` calling `store.add_relation`
  with an explanation, a detector name and origin `inferred`.
