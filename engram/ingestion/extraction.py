"""Concept/entity extraction and knowledge-type classification.

Everything here is *heuristic and explainable* (no neural model): each classification
carries the markers that triggered it, so the user can see why a fragment was
labelled a Decision or an Idea and correct it.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from engram.core.textutil import STOPWORDS, concept_key, norm, strip_accents
from engram.ingestion.text import LABEL_RE

# ----------------------------------------------------------------- classification
_LABEL_KIND = {
    "decision": "decision", "decided": "decision", "idea": "idea", "pregunta": "question", "question": "question",
    "hecho": "fact", "fact": "fact", "evento": "event", "event": "event", "concepto": "concept", "concept": "concept",
    "observacion": "observation", "observation": "observation", "nota": "observation", "note": "observation",
    "proyecto": "project", "project": "project",
}

_RULES: dict[str, list[tuple[str, float]]] = {
    "decision": [
        (r"\bdecid(?:í|i|imos|ió|o|ido|ieron)\b", 0.75), (r"\bwe decided\b|\bi decided\b|\bdecided to\b", 0.75),
        (r"\b(?:elegimos|elegí|optamos|opté|escogimos)\b", 0.65), (r"\bwe (?:chose|picked|went with)\b", 0.65),
        (r"\b(?:abandon(?:é|e|amos|ar|ado|ed|ing)?|descart(?:é|e|amos|ar|ado)|dej(?:é|amos) de usar)\b", 0.65),
        (r"\b(?:migrar|migramos|migré|migrate[ds]?|switch(?:ed)? to|cambiamos a|cambié a|pasamos a)\b", 0.5),
        (r"\b(?:vamos a usar|usaremos|will use|we'll use|going with|nos quedamos con)\b", 0.55),
        (r"\b(?:resolución|resolvimos|acordamos|agreed)\b", 0.5),
    ],
    "idea": [
        (r"\bidea\b", 0.5), (r"\b(?:podría(?:mos)?|could build|could we|we could|i could)\b", 0.45),
        (r"\b(?:qué tal si|what if|y si)\b", 0.6), (r"\b(?:me gustaría|would be (?:cool|nice|great)|sería (?:genial|útil|interesante))\b", 0.55),
        (r"\b(?:propuesta|propongo|proposal|propose)\b", 0.45), (r"\b(?:brainstorm|lluvia de ideas)\b", 0.5),
    ],
    "question": [(r"¿[^?]{3,}\?", 0.7), (r"\?\s*$", 0.55), (r"\b(?:pregunta abierta|open question|no sé si|i wonder)\b", 0.55)],
    "project": [
        (r"\b(?:proyecto|project)\b", 0.35), (r"\b(?:roadmap|milestone|hito|entregable|deliverable|sprint|mvp)\b", 0.45),
        (r"\b(?:objetivo del proyecto|project goal|alcance|scope)\b", 0.45),
    ],
    "event": [
        (r"\b(?:reunión|meeting|llamada|call with|conferencia|lanzamos|launched|ocurrió|happened|incidente|incident)\b", 0.55),
        (r"\b(?:ayer|hoy|today|yesterday|esta mañana|this morning)\b", 0.25),
    ],
    "concept": [
        (r"^\s*[\w\s\-]{2,40}\s(?:es un|es una|son|is an?|are|se define como|refers to|means|significa)\b", 0.5),
        (r"\b(?:definición|definition|en otras palabras|in other words)\b", 0.45),
    ],
    "observation": [
        (r"\b(?:noté|observé|me di cuenta|i noticed|i observed|parece que|it seems|seems like|looks like|aprendí|i learned)\b", 0.5),
        (r"\b(?:resultado|results?|medimos|we measured|benchmark)\b", 0.35),
    ],
}
_COMPILED = {k: [(re.compile(p, re.I | re.M), w) for p, w in v] for k, v in _RULES.items()}


@dataclass
class Classification:
    kind: str
    confidence: float
    reason: str


_REPORTED = re.compile(r"(?:qu[eé]|lo que|what|whether|si)\s+(?:\w+\s+)?$", re.I)
# "2024-07-01 — Idea: …" : a journal date in front of an explicit label
_DATE_PREFIX = re.compile(r"^\s*(?:[-*+]\s+)?[\d/.\-]{8,10}\s*[—–:\-]+\s*")


def classify(text: str, default_kind: str | None = None, has_date: bool = False) -> Classification:
    m = LABEL_RE.match(_DATE_PREFIX.sub("", text, count=1))
    if m:
        label = norm(m.group(1))
        kind = _LABEL_KIND.get(label)
        if kind:
            return Classification(kind, 0.95, f"explicit label '{m.group(1)}:' in source")
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for kind, rules in _COMPILED.items():
        for rx, w in rules:
            hit = rx.search(text)
            if hit and kind == "decision" and _REPORTED.search(text[max(0, hit.start() - 12) : hit.start()]):
                hit = None  # "qué decidimos…", "what we decided…": a mention, not a decision
            if hit:
                scores[kind] = scores.get(kind, 0.0) + w
                reasons.setdefault(kind, []).append(f"'{hit.group(0).strip()[:30]}'")
    if "question" in scores and len(text) > 400:
        scores["question"] *= 0.5  # a long passage with a question mark is rarely just a question
    if has_date and "event" in scores:
        scores["event"] += 0.25
        reasons["event"].append("explicit date")
    if scores:
        kind = max(scores, key=scores.get)
        conf = min(0.9, 0.35 + scores[kind] * 0.5)
        if default_kind and default_kind != kind and conf < 0.6:
            return Classification(default_kind, 0.6, f"document type '{default_kind}' (front-matter); weak markers for {kind}")
        return Classification(kind, round(conf, 2), "markers: " + ", ".join(reasons[kind][:4]))
    if default_kind:
        return Classification(default_kind, 0.6, f"document type '{default_kind}' (front-matter)")
    return Classification("fact", 0.3, "no specific markers (default type)")


# ----------------------------------------------------------------- concepts & entities
_PHRASE_SPLIT = re.compile(r"[\n.,;:!?¿¡()\[\]{}\"“”«»|/\\*_`>#=+]+|\s[-–—]\s")
_WORD = re.compile(r"[^\W\d_][\w\-']*", re.UNICODE)
_ENTITY = re.compile(
    r"\b(?:[A-ZÁÉÍÓÚÑ][\w\-\.]*[A-Z0-9][\w\-]*"                      # CamelCase / mixed: FastAPI, SQLite, GPT-4
    r"|[A-Z]{2,8}s?\b"                                               # ACRONYMS
    r"|[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+(?:de\s+|del\s+|of\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3}"  # Multi Word Names
    r"|[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})"                                # Single capitalised word (filtered below)
)
_ENTITY_STOP = {norm(w) for w in (
    "El La Los Las Un Una The A An This That These Those En Por Para Con Sin Si No Yo We I It "
    "Ok OK TODO NOTE Nota Idea Decisión Decision Pregunta Question Proyecto Project Hecho Fact Evento Event "
    "Concepto Concept Observación Observation Lunes Martes Miércoles Jueves Viernes Sábado Domingo "
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday PDF URL HTTP HTTPS API Ver See Véase Usar Use "
    "Hoy Ayer Today Yesterday Además También However Also Pero But Luego Then".split()
)}
_MONTH_WORDS = {"enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
                "noviembre", "diciembre", "january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december"}


# Words that make poor concepts: conjugated verbs and very generic nouns.
_VERB_ENDINGS = (
    "amos", "imos", "emos", "aron", "ieron", "aba", "aban", "ábamos", "abamos", "íamos", "iamos", "ían", "ian",
    "aría", "ería", "iría", "arías", "erías", "ando", "iendo", "ado", "ida", "ido", "aste", "iste", "ed",
)
_GENERIC = {norm(w) for w in (
    "lugar detalles detalle cosa cosas parte partes ejemplo ejemplos caso casos tiempo minutos minuto horas hora días día "
    "año años semana semanas mes meses vez veces forma formas tipo tipos problema problemas tema temas punto puntos "
    "idea ideas decisión decisiones pregunta preguntas nota notas hecho evento concepto observación proyecto "
    "example examples case cases time minutes hours days day year years week weeks way ways kind thing point points "
    "note notes decision decisions question questions fact event concept observation project idea ideas "
    "move moves go goes put take keep try start stop usar crear hacer tener poder deber querer ver decir use create make need want see say build "
    "creo pienso think deberíamos debería podemos puedo quiero queremos tenemos necesito necesitamos "
    "debe deben hará harán dentro demasiado demasiada mediante vive guarda necesita tarda consumía permite ofrece "
    "aportan exigen frente único única cualquier cada podría podrían recuerde abra empieza modo cero"
).split()}


_VERB_ENDINGS_N = tuple(strip_accents(e) for e in _VERB_ENDINGS)


def _is_verbish(n: str) -> bool:
    if n in _GENERIC:
        return True
    return len(n) > 5 and n.endswith(_VERB_ENDINGS_N)


@dataclass
class Extracted:
    concepts: list[tuple[str, float]] = field(default_factory=list)   # (display name, weight)
    entities: list[tuple[str, float]] = field(default_factory=list)


def extract_entities(text: str, limit: int = 8) -> list[tuple[str, float]]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for m in _ENTITY.finditer(text):
        name = m.group(0).strip(" .-")
        key = norm(name)
        if len(key) < 2 or key in _ENTITY_STOP or key in STOPWORDS or key in _MONTH_WORDS:
            continue
        words = name.split()
        # A single Capitalised word at the start of a sentence is just grammar, not an entity.
        if len(words) == 1 and name[:1].isupper() and name[1:].islower():
            before = text[: m.start()].rstrip()
            if not before or before[-1] in ".!?:\n¿¡\"'-*#>—–":
                continue
        if len(words) > 1 and norm(words[0]) in _ENTITY_STOP:
            continue
        counts[key] += 1
        display.setdefault(key, name)
    return [(display[k], float(c)) for k, c in counts.most_common(limit)]


def extract_concepts(text: str, limit: int = 6) -> list[tuple[str, float]]:
    """RAKE-style key phrases: candidate phrases are runs of content words between stop-words/punctuation."""
    phrases: list[list[str]] = []
    for part in _PHRASE_SPLIT.split(text):
        current: list[str] = []
        for w in _WORD.findall(part):
            n = norm(w)
            if n in STOPWORDS or len(n) < 3 or n in _MONTH_WORDS or n.startswith("http") or _is_verbish(n):
                if current:
                    phrases.append(current)
                current = []
            else:
                current.append(w)
        if current:
            phrases.append(current)
    # Long RAKE phrases are usually clauses, not concepts: split them into pairs.
    phrases = [p[i : i + 2] for p in phrases if p for i in range(0, len(p), 2)]
    if not phrases:
        return []
    freq: Counter[str] = Counter()
    degree: Counter[str] = Counter()
    for p in phrases:
        for w in p:
            k = concept_key(w)
            freq[k] += 1
            degree[k] += len(p) - 1
    word_score = {w: (degree[w] + freq[w]) / freq[w] for w in freq}
    scored: dict[str, float] = {}
    display: dict[str, str] = {}
    for p in phrases:
        key = concept_key(" ".join(p))
        score = sum(word_score[concept_key(w)] for w in p)
        # frequent single words matter too
        if len(p) == 1:
            score += 0.5 * (freq[key] - 1)
        if key not in scored or score > scored[key]:
            scored[key] = score
            display.setdefault(key, " ".join(x.lower() if not x.isupper() else x for x in p))
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    out: list[tuple[str, float]] = []
    for key, score in ranked:
        if len(key) < 4:
            continue
        out.append((display[key], round(score, 2)))
        if len(out) >= limit:
            break
    return out


def extract(text: str, tags: list[str] | None = None) -> Extracted:
    text = LABEL_RE.sub("", text, count=1)
    clean = re.sub(r"```.*?```", " ", text, flags=re.S)
    clean = re.sub(r"https?://\S+", " ", clean)
    clean = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", clean)
    return Extracted(concepts=extract_concepts(clean), entities=extract_entities(clean))


__all__ = ["classify", "extract", "extract_concepts", "extract_entities", "Classification", "Extracted", "strip_accents"]
