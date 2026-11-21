import pytest

from engram.ingestion.extraction import classify, extract
from engram.intelligence.contradictions import _polarity, detect

POLARITY = [
    ("Decisión: usamos Redis como cola de mensajes y Postgres para guardar la memoria", "Redis", 1),
    ("Decisión: abandonamos la arquitectura de microservicios y pasamos a un monolito modular con SQLite, porque la "
     "complejidad operativa no compensaba para un solo desarrollador y Redis consumía demasiada memoria.", "Redis", -1),
    ("Decisión: abandonamos la arquitectura de microservicios y pasamos a un monolito modular con SQLite, porque", "SQLite", 1),
    ("Decisión: abandonamos la arquitectura de microservicios y pasamos a un monolito", "microservicios", -1),
    ("Tras abandonar los microservicios, el monolito con SQLite arranca en 3 segundos y usa 300 MB de RAM.", "SQLite", 1),
    ("La búsqueda vectorial se hará dentro de SQLite en lugar de Postgres con pgvector.", "Postgres", -1),
    ("La búsqueda vectorial se hará dentro de SQLite en lugar de Postgres con pgvector.", "SQLite", 1),
    ("Redis fue descartado tras las pruebas.", "Redis", -1),
    ("We stopped using Docker for local development.", "Docker", -1),
    ("We use Docker for local development.", "Docker", 1),
    ("No vamos a usar Kubernetes.", "Kubernetes", -1),
    ("Seguimos con SQLite.", "SQLite", 1),
    ("El orquestador se divide en microservicios.", "microservicios", 0),
]


@pytest.mark.parametrize("sentence,concept,expected", POLARITY)
def test_polarity(sentence, concept, expected):
    assert _polarity(sentence, concept) == expected


def test_detect_contradiction_and_values():
    sig = detect("Usamos Redis para la cola.", "Abandonamos Redis porque consumía demasiada memoria.", ["Redis"])
    assert sig and sig[0].kind == "polarity"
    sig = detect("El servidor tiene 8 GB de RAM.", "El servidor tiene 16 GB de RAM.", ["servidor"])
    assert sig and sig[0].kind == "values"
    assert detect("Usamos Redis.", "Usamos Redis también aquí.", ["Redis"]) == []


@pytest.mark.parametrize("text,kind", [
    ("Decisión: migrar a SQLite.", "decision"),
    ("2024-07-01 — Idea: usar event sourcing.", "idea"),
    ("¿Deberíamos volver a una arquitectura distribuida?", "question"),
    ("We decided to drop the cache layer.", "decision"),
    ("Cada conversación empieza de cero y el agente no sabe qué decidimos la semana pasada.", "fact"),
    ("Noté que Redis se queda sin memoria con repositorios grandes.", "observation"),
    ("Reunión con el equipo de producto para revisar el roadmap.", "event"),
])
def test_classification(text, kind):
    c = classify(text, has_date=False)
    assert c.kind == kind, c
    assert c.reason


def test_extraction_prefers_entities_and_nouns():
    ex = extract("Construimos el orquestador con LangChain y Redis como cola de mensajes.")
    names = {n for n, _ in ex.entities}
    assert {"LangChain", "Redis"} <= names
    concepts = {n for n, _ in ex.concepts}
    assert "orquestador" in concepts
    assert "construimos" not in concepts  # conjugated verbs are not concepts


def test_relations_are_typed_explained_and_proposed(engine, demo_dir):
    engine.ingest(demo_dir)
    rels = engine.store.list_relations(limit=1000)
    assert rels
    for r in rels:
        assert r.explanation and r.type and r.detector
        if r.origin.value == "inferred":
            assert r.status.value == "proposed"   # never silently accepted
    sim = [r for r in rels if r.type.value == "semantically_similar"]
    assert all("not a documented link" in r.explanation for r in sim)
    assert all("lexical" in r.explanation for r in sim)  # hash provider is labelled non-neural


def test_consolidation_is_non_destructive(engine, demo_dir):
    engine.ingest(demo_dir)
    before = engine.store.stats()
    rep = engine.consolidate()
    after = engine.store.stats()
    assert rep["destructive_actions"] == 0
    assert after["engrams"] == before["engrams"] and after["documents"] == before["documents"]
    assert after["relations"] >= before["relations"]
    # a rejected relation stays rejected after another consolidation pass
    r = engine.store.list_relations(origin="inferred")[0]
    from engram.memory.models import RelationStatus

    engine.store.set_relation_status(r.id, RelationStatus.REJECTED)
    engine.consolidate()
    assert engine.store.get_relation(r.id).status == RelationStatus.REJECTED


def test_insights_generated(engine, demo_dir):
    engine.ingest(demo_dir)
    engine.consolidate()
    kinds = {i["kind"] for i in engine.store.list_insights()}
    assert "theme" in kinds
    assert kinds & {"bridge", "contradiction"}
