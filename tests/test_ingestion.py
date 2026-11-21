from engram.ingestion.chunking import chunk_document
from engram.ingestion.markdown import parse_markdown
from engram.ingestion.metadata import filename_date, find_dates, pdf_date
from engram.ingestion.pdf import parse_pdf
from engram.ingestion.text import parse_text
from pdfgen import make_pdf

MD = """---
title: Plan
date: 2024-03-15
tags: [agentes, memoria]
type: project
---
# Plan

Primer párrafo sobre la herramienta. #local-first

Decisión: usar SQLite porque es ligero.
- Idea: exponer un servidor local.

## Revisión del 2 de abril de 2024

Ver [[Otra nota]] y [detalle](detalle.md).

```python
print("code is kept")
```
"""


def test_markdown_structure():
    d = parse_markdown(MD, "fallback")
    assert d.title == "Plan"
    assert d.authored_at == "2024-03-15" and "front-matter" in d.authored_at_source
    assert d.default_kind == "project"
    assert {"agentes", "memoria", "local-first"} <= set(d.tags)
    assert "Otra nota" in d.links and "detalle.md" in d.links
    texts = [d.text_of(b) for b in d.blocks]
    assert any(t.startswith("Decisión:") for t in texts)
    assert any(b.is_code for b in d.blocks)
    assert any(b.section == "Plan › Revisión del 2 de abril de 2024" for b in d.blocks)


def test_chunks_are_verbatim_spans():
    d = parse_markdown(MD, "x")
    for ch in chunk_document(d):
        assert MD[ch.start : ch.end] == d.units[None][ch.start : ch.end]
        assert MD[ch.start : ch.end].strip()


def test_labelled_items_are_atomic():
    d = parse_markdown(MD, "x")
    chunks = [MD[c.start : c.end] for c in chunk_document(d)]
    assert "Decisión: usar SQLite porque es ligero." in chunks
    assert "- Idea: exponer un servidor local." in chunks


def test_journal_entries_stay_separate():
    txt = "Diario\n\n2024-03-02 — Reunión.\n\n2024-05-20 — Noté un problema.\n\n2024-06-15 — Decidimos migrar."
    d = parse_text(txt, "f")
    assert d.title == "Diario"
    chunks = [txt[c.start : c.end] for c in chunk_document(d)]
    assert len(chunks) == 3 and all(c.startswith("2024-") for c in chunks)


def test_long_paragraph_is_split_at_sentences():
    txt = " ".join(f"Frase número {i} sobre arquitectura de software." for i in range(120))
    chunks = chunk_document(parse_text(txt, "f"), target=600, hard_max=900)
    assert len(chunks) > 3
    assert all(c.end - c.start <= 900 for c in chunks)
    assert all(txt[c.start : c.end].endswith(".") for c in chunks[:-1])


def test_pdf_pages_and_metadata():
    data = make_pdf([["Página uno con texto."], ["Página dos: decisión de usar SQLite."]], title="Doc", creation_date="20230105")
    d = parse_pdf(data, "fallback")
    assert d.title == "Doc"
    assert d.authored_at == "2023-01-05"
    assert {b.page for b in d.blocks} == {1, 2}
    assert "SQLite" in d.units[2]


def test_dates_are_never_invented():
    assert [f.value for f in find_dates("El 15 de marzo de 2024 y March 3, 2023 y 2022-12-01")] == ["2024-03-15", "2023-03-03", "2022-12-01"]
    assert [f.value for f in find_dates("en marzo de 2024")] == ["2024-03"]       # partial stays partial
    assert find_dates("versión 2.0 del 31/02/2024") == []                          # invalid date rejected
    assert find_dates("tengo 2024 razones") == []                                  # bare year is not a date
    assert pdf_date("D:20240420120000+01'00'") == "2024-04-20"
    assert filename_date("2024-01-10-ideas.md") == "2024-01-10"
    assert filename_date("ideas.md") is None


def test_ingest_dedup_and_versions(engine, tmp_path):
    f = tmp_path / "nota.md"
    f.write_text("# Nota\n\nUsamos Redis para la cola.", encoding="utf-8")
    r1 = engine.ingest(f)[0]
    r2 = engine.ingest(f)[0]
    assert r1.status == "ingested" and r2.status == "duplicate"
    f.write_text("# Nota\n\nUsamos Redis para la cola.\n\nAhora abandonamos Redis.", encoding="utf-8")
    r3 = engine.ingest(f)[0]
    assert r3.status == "ingested" and r3.previous_version_id == r1.document_id
    docs = engine.store.list_documents()
    assert len(docs) == 2  # the old version is kept
    # unchanged text carried between versions is not reported as a duplicate
    dups = engine.store.list_relations(types=["possible_duplicate"])
    assert dups == []


def test_engram_fields_and_provenance(engine, demo_dir):
    results = engine.ingest(demo_dir)
    assert all(r.status == "ingested" for r in results), [r.error for r in results]
    items, total = engine.store.list_engrams(limit=500)
    assert total >= 15
    for g in items:
        doc = engine.store.get_document(g.provenance.document_id)
        assert g.uid.startswith("eg_") and g.content_hash and g.ingested_at
        assert g.kind.value in {"fact", "idea", "event", "decision", "concept", "observation", "question", "project"}
        if g.occurred_at:
            assert g.occurred_at_source
        if g.created_at:
            assert g.created_at_source
        # every engram maps back to the exact bytes of its stored original
        path, _ = engine.original_file(doc.id)
        if doc.kind != "pdf":
            raw = path.read_bytes().decode("utf-8")
            assert raw[g.provenance.char_start : g.provenance.char_end] == g.content
    pdf_engrams = [g for g in items if g.provenance.filename.endswith(".pdf")]
    assert pdf_engrams and all(g.provenance.page for g in pdf_engrams)
    kinds = {g.kind.value for g in items}
    assert {"decision", "idea", "question"} <= kinds


def test_explicit_wiki_link_becomes_reference(engine, demo_dir):
    engine.ingest(demo_dir)
    refs = engine.store.list_relations(types=["references"])
    assert refs and all(r.origin.value == "explicit" and r.status.value == "confirmed" for r in refs)
