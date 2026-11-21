import time
import tracemalloc
import zipfile

import pytest
from fastapi.testclient import TestClient

from engram.api.app import create_app
from engram.core.config import load_settings
from engram.core.engine import EngramEngine


def test_memories_survive_restart(settings, demo_dir):
    e1 = EngramEngine(settings)
    e1.ingest(demo_dir)
    stats1 = e1.store.stats()
    first = e1.search("Redis")["results"][0]["engram"]["id"]
    e1.close()

    e2 = EngramEngine(load_settings(data_dir=settings.data_dir, embedding_provider="hash"))
    try:
        stats2 = e2.store.stats()
        for k in ("documents", "engrams", "relations", "concepts", "embeddings"):
            assert stats2[k] == stats1[k], k
        assert e2.search("Redis")["results"][0]["engram"]["id"] == first
        # re-importing after restart is still recognised as already known
        assert all(r.status == "duplicate" for r in e2.ingest(demo_dir))
    finally:
        e2.close()


def test_backup_and_export(engine, demo_dir):
    engine.ingest(demo_dir)
    b = engine.backup()
    with zipfile.ZipFile(b) as z:
        names = z.namelist()
    assert "engram.db" in names and any(n.startswith("sources/") for n in names)
    x = engine.export_json()
    import json

    data = json.loads(x.read_text("utf-8"))
    assert len(data["engrams"]) == engine.store.stats()["engrams"]


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as c:
        yield c


def _wait(client, job_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.1)
    raise AssertionError("job timeout")


def test_api_full_flow(client, demo_dir):
    files = [("files", (p.name, p.read_bytes())) for p in sorted(demo_dir.iterdir())]
    r = client.post("/api/ingest", files=files)
    assert r.status_code == 200
    jobs = [_wait(client, j["job_id"]) for j in r.json()["jobs"]]
    assert all(j["status"] == "done" for j in jobs), jobs
    ov = client.get("/api/overview").json()
    assert ov["stats"]["engrams"] > 10 and ov["stats"]["documents"] == 4
    s = client.get("/api/search", params={"q": "herramienta para agentes"}).json()
    eid = s["results"][0]["engram"]["id"]
    d = client.get(f"/api/engrams/{eid}").json()
    assert d["engram"]["provenance"]["filename"] and "relations" in d
    rec = client.post("/api/reconstruct", json={"question": "¿Por qué abandoné mi anterior arquitectura?"}).json()
    assert rec["evidence"]
    g = client.get("/api/graph").json()
    assert g["nodes"] and g["edges"]
    tl = client.get("/api/timeline").json()
    assert tl["items"]
    rel = client.get("/api/relations", params={"origin": "inferred"}).json()[0]
    assert client.patch(f"/api/relations/{rel['id']}", json={"status": "confirmed"}).json()["status"] == "confirmed"
    structural = client.get("/api/relations", params={"origin": "structural"}).json()[0]
    assert client.patch(f"/api/relations/{structural['id']}", json={"status": "rejected"}).status_code == 409
    orig = client.get(f"/api/documents/{d['engram']['provenance']['document_id']}/original")
    assert orig.status_code == 200 and orig.headers["content-type"] == "application/octet-stream"
    assert client.post("/api/consolidate").json()["destructive_actions"] == 0
    assert client.get("/api/insights").status_code == 200


def test_api_security(client, engine):
    r = client.get("/api/status")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/documents/999/original").status_code == 404
    assert client.get("/..%2f..%2fpyproject.toml").status_code == 404
    assert client.get("/api/engrams", params={"kinds": "evil"}).status_code == 422
    # disallowed type: accepted as upload, rejected by ingestion with a clear error
    j = client.post("/api/ingest", files=[("files", ("x.exe", b"MZ\x00\x00"))]).json()["jobs"][0]
    res = _wait(client, j["job_id"])
    assert res["status"] == "failed" and "not allowed" in res["error"]
    # path in the uploaded filename is stripped
    j = client.post("/api/ingest", files=[("files", ("../../escape.md", b"# t\n\nhola mundo de prueba"))]).json()["jobs"][0]
    assert j["filename"] == "escape.md"
    _wait(client, j["job_id"])
    assert not (engine.settings.data_dir.parent / "escape.md").exists()


def test_upload_size_limit(tmp_path):
    s = load_settings(data_dir=tmp_path / "d", embedding_provider="hash", max_file_bytes=1000)
    e = EngramEngine(s)
    try:
        with TestClient(create_app(e)) as c:
            r = c.post("/api/ingest", files=[("files", ("big.txt", b"a" * 5000))])
            assert r.status_code == 413
    finally:
        e.close()


def test_performance_budget(engine, tmp_path):
    """~300 paragraphs across 30 files ingest quickly with modest memory (hash provider, CPU only)."""
    words = "arquitectura memoria agente decisión herramienta servidor contexto proyecto idea sistema datos".split()
    for f in range(30):
        paras = [" ".join(words[(f + i + j) % len(words)] for j in range(40)) + "." for i in range(10)]
        (tmp_path / f"n{f}.md").write_text(f"# Nota {f}\n\n" + "\n\n".join(paras), encoding="utf-8")
    tracemalloc.start()
    t0 = time.perf_counter()
    res = engine.ingest(tmp_path, recursive=False)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sum(r.status == "ingested" for r in res) == 30
    assert elapsed < 60, elapsed
    assert peak < 200 * 1024 * 1024, peak
    t0 = time.perf_counter()
    engine.search("decisión sobre la arquitectura del servidor")
    assert time.perf_counter() - t0 < 2
