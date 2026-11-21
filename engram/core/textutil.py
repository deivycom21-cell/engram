"""Small, dependency-free text utilities shared by ingestion and retrieval (Spanish + English)."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

STOPWORDS_ES = set("""
a al algo algún alguna algunas alguno algunos ante antes aquel aquella aquellas aquello aquellos aquí así aun aunque
bajo bien cada casi como cómo con contra cual cuál cuales cuáles cuando cuándo cuanto cuánto de del desde donde dónde
dos durante e el él ella ellas ello ellos en entre era eran eres es esa esas ese eso esos esta está estaba estaban
estado estamos están estar estas este esto estos estoy fue fueron fui ha había habían han has hasta hay he hemos
hice hizo la las le les lo los más me mi mí mis mismo mucho muchos muy nada ni no nos nosotros nuestra nuestro
nuestros o otra otras otro otros para pero poco por porque qué que quien quién quienes se sea según ser si sí
siempre sin sino sobre solo sólo son su sus también tan tanto te tener tengo tiene tienen todo todos tu tú tus un
una uno unos usted vez y ya yo hacer hace puede pueden ser será sería serían cosa cosas manera forma tipo parte
mientras luego entonces además aún dicho cual vamos voy va van sido tenía tuve tuvo había hubo tras hacia día
""".split())

STOPWORDS_EN = set("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers him
his how i if in into is it its itself just me more most my no nor not now of off on once only or other our ours out
over own same she should so some such than that the their theirs them then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your yours yourself
much many really thing things way lot get got make made one two use used using via etc may might must shall
""".split())

WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


_WS = re.compile(r"\s+")


def strip_accents(s: str) -> str:
    if s.isascii():
        return s
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


@lru_cache(maxsize=65536)
def norm(s: str) -> str:
    """Normalisation key: lower case, no accents, collapsed whitespace (memoised: hot path)."""
    return _WS.sub(" ", strip_accents(s.lower())).strip()


STOPWORDS = {norm(w) for w in STOPWORDS_ES | STOPWORDS_EN}


def light_stem(word: str) -> str:
    """Very light plural folding for ES/EN keys (agentes->agente, decisiones->decision, agents->agent)."""
    w = word
    if len(w) > 6 and w.endswith("iones"):
        return w[:-2]
    if len(w) > 5 and w.endswith("es") and w[-3] in "rlndjz":
        return w[:-2]
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


@lru_cache(maxsize=65536)
def concept_key(phrase: str) -> str:
    return " ".join(light_stem(w) for w in norm(phrase).split())


def tokens(text: str) -> list[str]:
    return WORD_RE.findall(text)


def content_words(text: str, min_len: int = 3) -> list[str]:
    return [t for t in (norm(w) for w in tokens(text)) if len(t) >= min_len and t not in STOPWORDS and not t.isdigit()]
