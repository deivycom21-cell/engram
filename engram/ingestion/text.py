"""Plain-text parser: paragraphs separated by blank lines."""
from __future__ import annotations

import re

from engram.ingestion.parsed import Block, ParsedDocument

LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?"
    r"(decisi[oó]n|decision|decided|idea|pregunta|question|hecho|fact|evento|event|concepto|concept|"
    r"observaci[oó]n|observation|proyecto|project|nota|note)(?:\*\*|__)?\s*[:：]",
    re.I,
)

_PARA = re.compile(r"\n[ \t]*\n+")
# A paragraph/line that *starts* with a date is a dated entry (journal, log): keep it separate.
DATE_LEAD = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|"
    r"\d{1,2}\s+(?:de\s+)?[A-Za-zÁÉÍÓÚáéíóú]{3,10}\.?\s+(?:de\s+)?\d{4})\b"
)


def is_atomic(line: str) -> bool:
    return bool(LABEL_RE.match(line) or DATE_LEAD.match(line))


def paragraph_spans(text: str, offset: int = 0) -> list[tuple[int, int]]:
    """Return (start, end) spans of non-empty paragraphs, trimmed of surrounding whitespace."""
    spans = []
    pos = 0
    for m in list(_PARA.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        seg = text[pos:end]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            trail = len(seg) - len(seg.rstrip())
            spans.append((offset + pos + lead, offset + end - trail))
        if m:
            pos = m.end()
    return spans


def split_labelled_lines(text: str, start: int, end: int) -> list[tuple[int, int, bool]]:
    """Inside a paragraph, isolate lines that carry an explicit label (e.g. "Decisión: ...").

    Returns (start, end, atomic) sub-spans covering the paragraph.
    """
    seg = text[start:end]
    lines = seg.split("\n")
    if len(lines) == 1 or not any(is_atomic(l) for l in lines):
        return [(start, end, is_atomic(seg))]
    out: list[tuple[int, int, bool]] = []
    cursor = start
    buf_start = None
    for line in lines:
        l_start, l_end = cursor, cursor + len(line)
        cursor = l_end + 1
        if is_atomic(line):
            if buf_start is not None:
                out.append((buf_start, l_start - 1, False))
                buf_start = None
            out.append((l_start, l_end, True))
        elif line.strip():
            # continuation lines (indented) belong to the previous labelled item
            if out and out[-1][2] and line.startswith((" ", "\t")) and buf_start is None:
                s, _, a = out.pop()
                out.append((s, l_end, a))
            elif buf_start is None:
                buf_start = l_start
    if buf_start is not None:
        out.append((buf_start, end, False))
    return [(s, e, a) for s, e, a in out if text[s:e].strip()]


def parse_text(text: str, title: str) -> ParsedDocument:
    blocks: list[Block] = []
    spans = paragraph_spans(text)
    # A short first line with no sentence punctuation is the document's title, not a memory.
    if len(spans) > 1:
        s, e = spans[0]
        first = text[s:e]
        if "\n" not in first and len(first) <= 80 and not first.rstrip().endswith((".", "?", "!", ":")) \
                and not is_atomic(first):
            title = first.strip()
            spans = spans[1:]
    for s, e in spans:
        for ss, ee, atomic in split_labelled_lines(text, s, e):
            blocks.append(Block(start=ss, end=ee, atomic=atomic))
    return ParsedDocument(title=title, kind="text", units={None: text}, blocks=blocks)
