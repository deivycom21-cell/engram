"""Natural-language query analysis: content terms, temporal intent and knowledge-type hints."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from engram.core.textutil import STOPWORDS, norm, tokens
from engram.ingestion.metadata import find_dates

# Words that describe the *act of remembering*, not the topic being remembered.
META_WORDS = {norm(w) for w in (
    "pensado pense pensé pensaba pensabamos pensábamos pienso recordar recuerdo recuerdas acuerdo anteriormente antes "
    "previamente originalmente inicialmente alguna vez sobre acerca respecto tema temas dije escribí escribi anoté anote "
    "think thought thinking remember recall previously before earlier originally initially ever about regarding wrote "
    "noted said mine my mi mis yo i what que qué cual cuál cómo como cuando cuándo why por porque how when where donde dónde "
    "cosas thing things algo something anterior previous old viejo vieja antigua antiguo latest ultimo último ultima última "
    "reciente recent current actual ahora now hoy today"
).split()}

KIND_HINTS = {
    "decision": r"\b(decid\w*|decision\w*|decisión|elegí|elegimos|abandon\w*|descart\w*|chose|choice|why did i|por qu[eé] (?:abandon|dej|cambi|eleg|decid))",
    "idea": r"\b(idea\w*|pensad\w*|pensé|pense|ocurri[oó]|brainstorm|what if|qu[eé] tal si|propuesta)\b",
    "question": r"\b(pregunta\w*|question\w*|duda\w*|open issue)\b",
    "project": r"\b(proyecto\w*|project\w*|roadmap|mvp)\b",
    "event": r"\b(reuni[oó]n|meeting|evento|event|pas[oó]|happened|ocurri[oó])\b",
}
OLDER = re.compile(r"\b(anteriormente|antes|previamente|originalmente|al principio|inicialmente|anterior|previous(?:ly)?|before|earlier|originally|initially|at first|old|antigu[oa]|viej[oa]|primera vez|first time)\b", re.I)
NEWER = re.compile(r"\b(ahora|actual(?:mente)?|reciente(?:mente)?|[uú]ltim[oa]s?|latest|recent(?:ly)?|current(?:ly)?|now|today|hoy)\b", re.I)
WHY = re.compile(r"\b(por qu[eé]|porqu[eé]|why|raz[oó]n|motivo|reason)\b", re.I)
YEAR = re.compile(r"\b(?:en|in|del|de|during|durante)?\s*((?:19|20)\d{2})\b")


@dataclass
class ParsedQuery:
    raw: str
    terms: list[str]                      # content terms for full-text search
    kind_hints: list[str] = field(default_factory=list)
    prefer: str | None = None             # older | newer | None
    date_from: str | None = None
    date_to: str | None = None
    asks_why: bool = False

    @property
    def fts_query(self) -> str | None:
        """Safe FTS5 expression: quoted prefix terms joined by OR (user input never reaches FTS syntax raw)."""
        parts = []
        for t in self.terms:
            stem = t[: max(4, min(len(t), 6))] if len(t) > 6 else t
            stem = re.sub(r"[^\w]", "", stem)
            if stem:
                parts.append(f'"{stem}"*')
        return " OR ".join(dict.fromkeys(parts)) or None


def parse_query(q: str) -> ParsedQuery:
    q = q.strip()[:1000]
    terms = []
    for t in tokens(q):
        n = norm(t)
        if len(n) < 3 or n in STOPWORDS or n in META_WORDS or n.isdigit():
            continue
        terms.append(n)
    hints = [k for k, rx in KIND_HINTS.items() if re.search(rx, q, re.I)]
    prefer = "older" if OLDER.search(q) else "newer" if NEWER.search(q) else None
    date_from = date_to = None
    dates = find_dates(q, 2)
    if dates:
        d = dates[0].value
        date_from, date_to = d, d + "~"
    else:
        years = YEAR.findall(q)
        if years:
            y = min(years)
            date_from, date_to = f"{y}", f"{max(years)}~"
    return ParsedQuery(raw=q, terms=list(dict.fromkeys(terms)), kind_hints=hints, prefer=prefer,
                       date_from=date_from, date_to=date_to, asks_why=bool(WHY.search(q)))
