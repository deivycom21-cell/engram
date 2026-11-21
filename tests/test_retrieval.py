from engram.retrieval.query import parse_query


def test_query_analysis():
    pq = parse_query("¿Qué había pensado anteriormente sobre construir una herramienta para agentes de programación?")
    assert pq.terms == ["construir", "herramienta", "agentes", "programacion"]
    assert pq.prefer == "older" and "idea" in pq.kind_hints
    pq = parse_query("¿Por qué abandoné la arquitectura en 2024?")
    assert pq.asks_why and "decision" in pq.kind_hints and pq.date_from == "2024"


def test_fts_query_is_sanitised(engine, demo_dir):
    engine.ingest(demo_dir)
    for nasty in ['"', "OR AND NOT", "redis* NEAR(", "a'); DROP TABLE engrams; --", "{}[]()^:", "ñandú ÆØÅ"]:
        res = engine.search(nasty)
        assert isinstance(res["results"], list)
    assert engine.store.stats()["engrams"] > 0


def test_contextual_recall_with_sources(engine, demo_dir):
    engine.ingest(demo_dir)
    res = engine.search("¿Qué había pensado sobre construir una herramienta para agentes de programación?")
    assert res["results"]
    top = res["results"][0]
    assert "herramienta" in top["engram"]["content"].lower()
    assert top["why"]
    p = top["engram"]["provenance"]
    assert p["document_title"] and p["filename"] and p["char_end"] > p["char_start"]


def test_graph_expansion_brings_related_memories(engine, demo_dir):
    engine.ingest(demo_dir)
    res = engine.search("Redis", limit=30)
    vias = [r for r in res["results"] if r["signals"]["via"]]
    assert vias and all(v["type"] for r in vias for v in r["signals"]["via"])


def test_kind_filter(engine, demo_dir):
    engine.ingest(demo_dir)
    res = engine.search("arquitectura", kinds=["decision"])
    assert res["results"] and all(r["engram"]["kind"] == "decision" for r in res["results"])


def test_reconstruction_is_verifiable(engine, demo_dir):
    engine.ingest(demo_dir)
    rec = engine.reconstruct("¿Por qué abandoné mi anterior arquitectura?")
    labels = {e["label"] for e in rec["evidence"]}
    assert labels and rec["unknowns"] and rec["summary"]
    assert rec["decisions"], "decision records must be surfaced"
    assert any("porque" in r["sentence"] for r in rec["reasons"])
    # every citation in the summary points to existing evidence
    import re

    for line in rec["summary"]:
        for lab in re.findall(r"\[(E\d+)\]", line):
            assert lab in labels
    # chronology is sorted and every date has its source
    dates = [c["date"] for c in rec["chronology"]]
    assert dates == sorted(dates)
    assert all(c["date_source"] for c in rec["chronology"])
    # evidence quotes are verbatim engrams
    for ev in rec["evidence"]:
        g = engine.store.get_engram(ev["engram"]["id"])
        assert g.content == ev["engram"]["content"]


def test_reconstruction_admits_ignorance(engine, demo_dir):
    engine.ingest(demo_dir)
    rec = engine.reconstruct("¿Qué opinaba de la cocina mediterránea y el aceite de oliva?")
    assert rec["retrieval_confidence"]["level"] in ("none", "low")
    assert rec["unknowns"]


def test_timeline_and_evolution(engine, demo_dir):
    engine.ingest(demo_dir)
    t = engine.temporal.timeline()
    dates = [i["date"] for i in t["items"]]
    assert dates == sorted(dates) and all(i["date_source"] for i in t["items"])
    ev = engine.evolution("Redis")
    assert ev["first_appearance"]["date"] <= ev["latest"]["date"]


def test_graph_endpoints(engine, demo_dir):
    engine.ingest(demo_dir)
    g = engine.graph.subgraph()
    ids = {n["id"] for n in g["nodes"]}
    assert g["nodes"] and g["edges"]
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])
    a, b = g["nodes"][0]["id"], g["nodes"][-1]["id"]
    path = engine.graph.path(a, b)
    assert path is None or all("relation" in s for s in path)
