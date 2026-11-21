"""The MVP acceptance flow with the real neural model (skipped if the model is not downloaded)."""
from engram.core.config import load_settings
from engram.core.engine import EngramEngine
from conftest import REPO_MODELS


def test_mvp_flow(neural_engine, demo_dir):
    e = neural_engine
    # 1-3. import three (here four) documents → persistent engrams
    results = e.ingest(demo_dir)
    assert [r.status for r in results] == ["ingested"] * 4
    # 4. embeddings generated with the neural provider
    st = e.store.stats()
    assert e.provider.neural and st["embeddings"] == st["engrams"]
    # 5. relations detected across documents, including the change of mind about Redis
    contra = e.store.list_relations(types=["possible_contradiction"])
    assert any("Redis" in r.explanation for r in contra)
    assert e.store.list_relations(types=["semantically_similar"])
    # 6-8. contextual question → relevant results with verifiable sources
    question = "¿Qué había pensado anteriormente sobre construir una herramienta para agentes de programación?"
    res = e.search(question)
    top = res["results"][0]["engram"]
    assert "herramienta" in top["content"] and top["provenance"]["filename"].startswith("2024-01-10")
    rec = e.reconstruct("¿Por qué abandoné mi anterior arquitectura?")
    assert rec["retrieval_confidence"]["level"] == "high"
    assert any("abandonamos la arquitectura" in d["excerpt"] for d in rec["decisions"])
    # unrelated questions return nothing rather than noise
    assert e.search("receta de tortilla de patatas")["results"] == []
    # 9. graph data for the 3D explorer
    g = e.graph.subgraph()
    assert len(g["nodes"]) == st["engrams"] and g["edges"]
    # 10. restart: memories persist
    data_dir = e.settings.data_dir
    e.close()
    e2 = EngramEngine(load_settings(data_dir=data_dir, embedding_provider="onnx", models_path=REPO_MODELS))
    try:
        assert e2.store.stats()["engrams"] == st["engrams"]
        assert e2.search(question)["results"][0]["engram"]["id"] == top["id"]
    finally:
        e2.close()
