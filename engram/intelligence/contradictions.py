"""Heuristic contradiction detection between two fragments that talk about the same topic.

This is deliberately conservative and transparent: it only flags *possible*
contradictions and always quotes the two sentences that triggered the flag.
Signals:
1. **Polarity** — one sentence adopts/affirms a shared concept, the other negates or
   abandons it ("usamos Redis" vs "abandonamos Redis").
2. **Values** — the same concept with the same unit but different numbers
   ("8 GB" vs "16 GB").
3. **Antonyms** — the same concept described with opposite qualifiers (fast/slow…).
No neural model is involved; NLI-grade detection is on the roadmap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from engram.core.textutil import concept_key, norm, strip_accents

_SENT = re.compile(r"(?<=[.!?\n])\s+")

SIMPLE_NEG = re.compile(r"\b(?:no|not|never|nunca|jamas|don't|doesn't|won't|didn't)\b", re.I)
NEGATIVE = re.compile(
    r"\b(?:ya no|dejamos de usar|dejamos de utilizar|deje de usar|dejamos de|deje de|stopped using|stop using|"
    r"stopped|no longer|"
    r"abandon\w*|descart\w*|rechaz\w*|reject\w*|deprecat\w*|elimin\w*|remov\w*|drop(?:ped)?|quit|"
    r"evitar|avoid\w*|sin usar|mala idea|bad idea|fracas\w*|fail\w*|problema con)\b",
    re.I,
)
# Negative *evaluations* next to the concept ("Redis consumía demasiada memoria").
PROBLEM = re.compile(
    r"\b(?:demasiad\w*|too much|too many|too slow|excesiv\w*|se queda sin|runs? out of|no compensa\w*|"
    r"not worth|inestable|unstable|se cae|crash\w*|lentisim\w*|bottleneck|cuello de botella)\b",
    re.I,
)
POSITIVE = re.compile(
    r"\b(?:usar|usamos|usaremos|uso|usa|utiliza\w*|use[ds]?|using|adopt\w*|eleg\w*|elij\w*|chose|choose|choosing|optamos|opté|"
    r"mantener|mantenemos|keep|keeping|recomiend\w*|recommend\w*|funciona(?:\s+bien)?|works(?:\s+well)?|"
    r"apostamos|seguir con|stick with|go with|going with|implement\w*|integr\w*|migrar a|migramos a|migrate to|pasar a|pasamos a|cambiamos a|seguimos con|switch to|switched to)\b",
    re.I,
)
# "X en lugar de Y" / "Y replaced by X": X is chosen, Y is rejected — decided per concept position.
SUBSTITUTION = re.compile(
    r"\b(?:en lugar de|en vez de|instead of|rather than|reemplaza\w*|sustitu\w*|replac\w*)\s+"
    r"(?:a |al |el |la |los |las |the |usar |using )?([\w\-]+)", re.I)
# Sentences are compared in normalised (accent-free, lower-case) form.
NEGATIVE = re.compile(strip_accents(NEGATIVE.pattern), re.I)
PROBLEM = re.compile(strip_accents(PROBLEM.pattern), re.I)
POSITIVE = re.compile(strip_accents(POSITIVE.pattern), re.I)

ANTONYMS = [
    ("rapido", "lento"), ("fast", "slow"), ("mejor", "peor"), ("better", "worse"), ("barato", "caro"),
    ("cheap", "expensive"), ("facil", "dificil"), ("easy", "hard"), ("simple", "complejo"), ("simple", "complex"),
    ("aumenta", "disminuye"), ("increase", "decrease"), ("seguro", "inseguro"), ("secure", "insecure"),
    ("estable", "inestable"), ("stable", "unstable"), ("util", "inutil"), ("useful", "useless"),
    ("suficiente", "insuficiente"), ("enough", "not enough"), ("ligero", "pesado"), ("lightweight", "heavy"),
]
_NUM_UNIT = re.compile(
    r"(\d+(?:[.,]\d+)?)\s?(gb|mb|kb|tb|ms|s|seg|segundos|seconds|min|minutos|minutes|h|horas|hours|%|€|eur|usd|\$|"
    r"usuarios|users|días|dias|days|semanas|weeks|meses|months|años|anos|years|cores|núcleos|nucleos)\b",
    re.I,
)


@dataclass
class ContradictionSignal:
    kind: str           # polarity | values | antonyms
    concept: str
    sentence_a: str
    sentence_b: str
    confidence: float
    explanation: str


def _split(text: str) -> list[tuple[str, str]]:
    """(raw sentence, stemmed key form) pairs, computed once per fragment."""
    out = []
    for raw in _SENT.split(text):
        raw = raw.strip()
        if raw:
            out.append((raw, " ".join(concept_key(w) for w in norm(raw).split())))
    return out


def _sentences_with(sentences: list[tuple[str, str]], key: str) -> list[str]:
    words = key.split()
    return [raw for raw, sk in sentences if all(w in sk for w in words)]


@lru_cache(maxsize=16384)
def _polarity(sentence: str, concept: str) -> int:
    """+1 adopts/affirms, -1 negates/abandons, 0 unclear — evaluated in a window around the concept."""
    n = norm(sentence)
    idx = n.find(norm(concept).split()[0][:5])
    window = n if idx < 0 else n[max(0, idx - 60) : idx + 60]
    before = n if idx < 0 else n[max(0, idx - 35) : idx]
    sub = SUBSTITUTION.search(n)
    if sub and idx >= 0:
        replaced = sub.group(1)
        if norm(concept).split()[0][:4] == replaced[:4]:
            return -1          # the option being replaced
        if idx < sub.start():
            return 1           # the option chosen instead
    if idx < 0:
        return 0
    # Markers reach their *direct object*: the concept follows within ~80 chars with no clause break
    # or opposite marker in between ("abandonamos X y pasamos a Y" → X rejected, Y adopted), or the
    # concept closely precedes the marker ("Redis fue descartado").
    neg_m = _direct(NEGATIVE, POSITIVE, n, idx)
    pos_m = _direct(POSITIVE, NEGATIVE, n, idx, skip_negated=True)
    neg = bool(neg_m) or bool(SIMPLE_NEG.search(before)) or bool(PROBLEM.search(n[max(0, idx - 50) : idx + 60]))
    pos = bool(pos_m)
    if neg and not pos:
        return -1
    if pos and not neg:
        return 1
    if neg_m and pos_m:  # both direct: the closer marker governs the concept
        return -1 if abs(idx - neg_m.end()) < abs(idx - pos_m.end()) else 1
    return 0


def _direct(markers: re.Pattern, blockers: re.Pattern, n: str, idx: int, skip_negated: bool = False):
    for m in markers.finditer(n):
        if skip_negated and (SIMPLE_NEG.search(n[max(0, m.start() - 15) : m.start()])
                             or NEGATIVE.search(n[max(0, m.start() - 15) : m.start()])):
            continue  # "no vamos a usar", "dejamos de usar": not an adoption
        if 0 <= idx - m.end() <= 80:
            between = n[m.end() : idx]
            if not re.search(r"[,;:]", between) and not blockers.search(between):
                return m
        elif 0 <= m.start() - idx <= 40 and not re.search(r"[,;:]", n[idx : m.start()]):
            return m
    return None


_ANT_WORDS = sorted({w for pair in ANTONYMS for w in pair}, key=len, reverse=True)
_ANT_ANY = re.compile(r"\b(" + "|".join(re.escape(w) for w in _ANT_WORDS) + r")")


@lru_cache(maxsize=8192)
def _antonym_words(sentence: str) -> frozenset:
    return frozenset(_ANT_ANY.findall(norm(sentence)))


def _antonym_hit(a: str, b: str) -> tuple[str, str] | None:
    wa, wb = _antonym_words(a), _antonym_words(b)
    if not wa or not wb:
        return None
    for x, y in ANTONYMS:
        if (x in wa and y in wb) or (y in wa and x in wb):
            return x, y
    return None


@lru_cache(maxsize=8192)
def _values(sentence: str) -> dict[str, frozenset]:
    out: dict[str, set[str]] = {}
    for num, unit in _NUM_UNIT.findall(sentence):
        out.setdefault(norm(unit), set()).add(num.replace(",", "."))
    return {u: frozenset(v) for u, v in out.items()}


def detect(text_a: str, text_b: str, shared_concepts: list[str]) -> list[ContradictionSignal]:
    signals: list[ContradictionSignal] = []
    sents_a, sents_b = _split(text_a), _split(text_b)
    for concept in shared_concepts[:8]:
        key = concept_key(concept)
        sa_list, sb_list = _sentences_with(sents_a, key), _sentences_with(sents_b, key)
        for sa in sa_list[:3]:
            for sb in sb_list[:3]:
                pa, pb = _polarity(sa, concept), _polarity(sb, concept)
                if pa and pb and pa != pb:
                    signals.append(ContradictionSignal(
                        "polarity", concept, sa, sb, 0.6,
                        f"One fragment {'adopts' if pa > 0 else 'rejects'} '{concept}' while the other "
                        f"{'adopts' if pb > 0 else 'rejects'} it.",
                    ))
                    continue
                va, vb = _values(sa), _values(sb)
                for unit in set(va) & set(vb):
                    if va[unit].isdisjoint(vb[unit]):
                        signals.append(ContradictionSignal(
                            "values", concept, sa, sb, 0.45,
                            f"Different values for '{concept}': {', '.join(sorted(va[unit]))} {unit} vs "
                            f"{', '.join(sorted(vb[unit]))} {unit}.",
                        ))
                        break
                ant = _antonym_hit(sa, sb)
                if ant:
                    signals.append(ContradictionSignal(
                        "antonyms", concept, sa, sb, 0.4,
                        f"'{concept}' is described with opposite qualifiers ('{ant[0]}' vs '{ant[1]}').",
                    ))
    # keep the strongest signal per concept
    best: dict[str, ContradictionSignal] = {}
    for s in signals:
        if s.concept not in best or s.confidence > best[s.concept].confidence:
            best[s.concept] = s
    return sorted(best.values(), key=lambda s: -s.confidence)
