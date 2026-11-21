"""Metadata extraction: front-matter and dates.

Rule: never invent a date. Every date returned carries the textual evidence it came
from (``source``). Partial dates are kept partial ("2024-03" or "2024") so ISO string
ordering still works and no fake day is fabricated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

MONTHS = {
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12, "ene": 1, "abr": 4, "ago": 8, "dic": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class FoundDate:
    value: str     # ISO, possibly partial: YYYY-MM-DD | YYYY-MM
    source: str    # human-readable evidence, e.g. "content: '15 de marzo de 2024'"
    start: int = 0


def _valid(y: int, m: int, d: int | None = None) -> bool:
    if not (1900 <= y <= 2100 and 1 <= m <= 12):
        return False
    if d is None:
        return True
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _iso(y: int, m: int, d: int | None = None) -> str:
    return f"{y:04d}-{m:02d}-{d:02d}" if d else f"{y:04d}-{m:02d}"


_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 2024-03-15, 2024/03/15, 2024.03.15 (optionally followed by a time)
    (re.compile(r"\b(?P<y>(?:19|20)\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\b"), "ymd"),
    # 15 de marzo de 2024 / 15 marzo 2024 / 15 March 2024
    (re.compile(rf"\b(?P<d>\d{{1,2}})(?:º|°)?\s+(?:de\s+)?(?P<mon>{_MONTH_ALT})\.?,?\s+(?:de(?:l)?\s+)?(?P<y>(?:19|20)\d{{2}})\b", re.I), "dmony"),
    # March 15, 2024
    (re.compile(rf"\b(?P<mon>{_MONTH_ALT})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>(?:19|20)\d{{2}})\b", re.I), "mondy"),
    # 15/03/2024 (day-first, the convention for Spanish; see DECISIONS D-05)
    (re.compile(r"\b(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})[/.-](?P<y>(?:19|20)\d{2})\b"), "dmy"),
    # marzo de 2024 / March 2024
    (re.compile(rf"\b(?P<mon>{_MONTH_ALT})\.?\s+(?:de(?:l)?\s+)?(?P<y>(?:19|20)\d{{2}})\b", re.I), "mony"),
]


def find_dates(text: str, limit: int = 5) -> list[FoundDate]:
    """Find explicit calendar dates in text (in order of appearance)."""
    found: list[FoundDate] = []
    taken: list[tuple[int, int]] = []
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            g = m.groupdict()
            y = int(g["y"])
            mon = MONTHS.get(g["mon"].lower().rstrip(".")) if g.get("mon") else int(g["m"])
            d = int(g["d"]) if g.get("d") else None
            if mon is None or not _valid(y, mon, d):
                continue
            note = ""
            if kind == "dmy" and d is not None and d <= 12 and mon <= 12 and d != mon:
                note = " (read as day/month/year)"
            found.append(FoundDate(_iso(y, mon, d), f"content: '{m.group(0)}'{note}", s))
            taken.append((s, e))
    found.sort(key=lambda f: f.start)
    return found[:limit]


def parse_loose_date(value: str) -> str | None:
    """Parse a metadata date value (front-matter etc.)."""
    value = str(value).strip().strip("'\"")
    m = re.match(r"^((?:19|20)\d{2})-(\d{2})-(\d{2})", value)
    if m and _valid(int(m[1]), int(m[2]), int(m[3])):
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"^((?:19|20)\d{2})-(\d{2})$", value)
    if m and _valid(int(m[1]), int(m[2])):
        return f"{m[1]}-{m[2]}"
    found = find_dates(value, 1)
    return found[0].value if found else None


def pdf_date(value: str | None) -> str | None:
    """PDF dates look like D:20230115120000+01'00'."""
    if not value:
        return None
    m = re.match(r"^D?:?((?:19|20)\d{2})(\d{2})?(\d{2})?", str(value))
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2] or 0), int(m[3] or 0)
    if mo and d and _valid(y, mo, d):
        return _iso(y, mo, d)
    if mo and _valid(y, mo):
        return _iso(y, mo)
    return None


def filename_date(filename: str) -> str | None:
    stem = Path(filename).stem
    m = re.search(r"((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", stem)
    if m and _valid(int(m[1]), int(m[2]), int(m[3])):
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    return None


def mtime_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- front-matter
_FM = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.S)


def parse_front_matter(text: str) -> tuple[dict, int]:
    """Parse a *minimal, safe* YAML subset (scalars and simple lists). Returns (data, end_offset).

    Deliberately not a full YAML parser: front-matter is untrusted input and only
    simple metadata (title, date, tags, type) matters here.
    """
    m = _FM.match(text)
    if not m:
        return {}, 0
    data: dict = {}
    current_list: str | None = None
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", raw)
        if item and current_list:
            data[current_list].append(item.group(1).strip().strip("'\""))
            continue
        kv = re.match(r"^([A-Za-z_][\w\- ]*):\s*(.*)$", raw)
        if not kv:
            continue
        key, val = kv.group(1).strip().lower(), kv.group(2).strip()
        if val == "":
            data[key] = []
            current_list = key
            continue
        current_list = None
        if val.startswith("[") and val.endswith("]"):
            data[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            data[key] = val.strip("'\"")
    return data, m.end()


FRONT_MATTER_DATE_KEYS = ("date", "created", "created_at", "fecha", "creado", "published")
