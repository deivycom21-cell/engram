# Technical decision log

Each entry records what was decided, why, and what was measured. Newest last.

## D-01 · SQLite as the single source of truth
Documents, engrams, concepts, relations, insights, jobs and the audit log live in one
SQLite file (WAL mode). Full-text search uses the built-in **FTS5** (`unicode61
remove_diacritics 2`, so `programación` matches `programacion`). No server, trivial
backups (`sqlite3.backup` API), works the same on Windows 11 and Kali.
*Rejected:* Postgres/pgvector, Qdrant, Chroma — each adds a process and hundreds of MB of RAM
for a single-user corpus that fits comfortably in SQLite.

## D-02 · sqlite-vec for k-NN, numpy as exact fallback
`sqlite-vec` (`vec0`, cosine) keeps vectors inside the same database. The `embeddings`
table (one row per engram *and* model) is the source of truth; `vec_index` is a derived
index rebuilt automatically when the active model changes. If the extension cannot load
(e.g. a Python whose `sqlite3` was built without extension support) a lazily loaded numpy
matrix gives exact brute-force search. Both paths are covered by the same API.

## D-03 · Neural vs. non-neural components, stated explicitly
Only the embedding model is a neural network. The graph is an ordinary adjacency table
traversed with BFS; classification, concept extraction, contradiction detection and
reconstruction are explainable heuristics. The UI and every explanation label which
signal produced a result ("semantic similarity" vs "lexical-vector similarity").

## D-04 · Embedding model: multilingual-e5-small, int8 ONNX, local
384 dimensions, 100+ languages (the user writes in Spanish and English), ~118 MB
download, runs on CPU with ONNX Runtime + `tokenizers` (no PyTorch, no
sentence-transformers). Measured on this machine (Windows 11, 4 cores): ~0.1 s per long
paragraph, ~515 MB process RAM with the model loaded. The CPU memory arena is disabled so
memory stays flat during long sessions (measured: +120 MB growth with arena, +0 without,
~13% slower).
*Fallback:* `hash` provider (feature hashing of words/char-trigrams) — deterministic,
zero download, clearly labelled non-neural. Used by default when the model is missing and
by the unit tests.

## D-05 · Dates are evidence, never guesses
Dates come only from: front-matter keys, PDF `CreationDate`, a date in the filename, a
date in the document title line, a date in the section heading, or a date written in the
fragment. Each stored date keeps its textual source. Partial dates stay partial
(`2024-02`), bare years are not dates, impossible dates are rejected. File modification
time is stored separately and never used as the date of an idea. `dd/mm/yyyy` is read
day-first (Spanish convention) and ambiguous cases are annotated "(read as day/month/year)".

## D-06 · Chunks are verbatim spans
Each engram is `source_text[char_start:char_end]` of the extracted text, so it can be
located exactly (tests assert this byte-for-byte for text/Markdown). Chunking respects
headings and PDF pages, packs small paragraphs up to ~900 chars, splits long ones at
sentence boundaries, and keeps explicitly labelled items (`Decisión:`, `Idea:`…) and
dated journal entries (`2024-05-20 — …`) as their own engrams.

## D-07 · Background-centred embeddings and calibrated thresholds
Problem measured: with raw e5 vectors an unrelated sentence ("receta de tortilla de
patatas") scored **0.876** cosine against the demo corpus, higher than many genuinely
related pairs (cross-document mean 0.852, σ 0.026). Absolute thresholds were meaningless.
Fix: subtract the mean of a fixed, topic-diverse background set (30 sentences in ES/EN,
independent of the user's data) per mode (query/passage) and re-normalise
("all-but-the-mean" anisotropy correction). After centring: related pairs 0.5–0.7,
unrelated 0.0–0.3, cross-doc mean 0.42 (σ 0.105). Thresholds: association 0.55, topic
0.45, duplicate 0.93 (plus ≥50% word overlap). The provider name carries the scheme
(`…-c1`) so vectors from different schemes are never mixed.

## D-08 · Inferred relations are typed proposals
Semantic similarity is stored as `semantically_similar` with its score and provider — it
is never presented as a factual link. Stronger types need extra evidence:
`possible_duplicate` (similarity *and* word overlap), `possible_contradiction` (shared
concept *and* opposite polarity / different values / antonyms, with both sentences
quoted), `later_version` (shared concept *and* similarity *and* dates a month+ apart,
compatible knowledge types, only the closest counterpart). Inferred relations start as
`proposed`; users confirm or reject. Rejected relations are kept so they are never
re-proposed. Explicit `[[wiki links]]` become confirmed `references`.

## D-09 · Contradiction polarity reaches only the direct object
First version applied "abandonamos" to every concept in the sentence and flagged
"Tras abandonar los microservicios, el monolito con SQLite…" as rejecting SQLite. Now a
negative/positive marker governs a concept only if it follows within ~80 chars with no
clause break or opposite marker in between, or closely precedes it ("Redis fue
descartado"); substitutions ("X en lugar de Y") mark Y rejected and X chosen; evaluative
problems ("consumía demasiada memoria") count as negative. 13 unit cases cover this.

## D-10 · Reconstruction is deterministic; narration is optional and fenced
The reconstruction assembles quotes, counts, dates and relations only, with evidence
labels in chronological order and an explicit "what cannot be determined" list. An LLM
narrative can be added (Ollama locally; Claude CLI only with `allow_external_llm = true`
*and* per-request consent). The model receives evidence in delimited blocks, is told the
content is data not instructions, gets no tools (`--tools ""`), and its citations are
checked against the evidence ids.

## D-11 · Performance: memoised normalisation, bounded pairwise checks
A 30-file synthetic corpus originally took 401 s to ingest: contradiction checks
normalised every sentence for every concept for every pair (29 M `strip_accents` calls).
Memoising `norm`/`concept_key`/polarity, pre-splitting sentences once per pair, one
combined antonym regex and capping topical partners to 12 per engram brought the same
8-file profile from 81.8 s to 1.9 s.

## D-12 · Local web stack without a build step
FastAPI + static ES modules, Three.js r160 vendored (no CDN: works offline and leaks no
requests). Strict CSP (`script-src 'self'`), `TrustedHostMiddleware` against DNS
rebinding, binding to 127.0.0.1 by default, all document text inserted with
`textContent`. Originals are served only as `application/octet-stream` downloads.

## D-13 · Versioning instead of overwriting
Same filename + different bytes = new document version linked by `previous_version_id`.
Old engrams stay searchable, unchanged text carried between versions is not reported as a
duplicate, and changed passages feed the contradiction / evolution detectors — the
history of a note becomes part of the memory.
