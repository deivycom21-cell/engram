"""Structure-aware chunking.

Chunks are *verbatim spans* of the extracted text (``content == unit_text[start:end]``),
so every engram can be located exactly in its source. Rules:

* never cross a page (PDF) or a section (Markdown heading) boundary;
* explicitly labelled items ("Decisión: …", "Idea: …") are their own engram;
* small neighbouring paragraphs are packed together up to ``target`` characters;
* oversized paragraphs are split at sentence boundaries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engram.ingestion.parsed import Block, ParsedDocument

_SENT_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+(?=[\"'(¿¡A-ZÁÉÍÓÚÑ0-9])|\n+")


@dataclass
class Chunk:
    start: int
    end: int
    page: int | None
    section: str | None
    atomic: bool = False


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    seg = text[start:end]
    spans, pos = [], 0
    for m in _SENT_END.finditer(seg):
        if m.start() > pos:
            spans.append((start + pos, start + m.start()))
        pos = m.end()
    if pos < len(seg):
        spans.append((start + pos, end))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _split_large(text: str, block: Block, target: int, hard_max: int) -> list[Chunk]:
    out: list[Chunk] = []
    cur_s = cur_e = None
    for s, e in _sentence_spans(text, block.start, block.end):
        # A single sentence longer than the hard max is cut at whitespace.
        while e - s > hard_max:
            cut = text.rfind(" ", s, s + hard_max)
            cut = cut if cut > s + hard_max // 2 else s + hard_max
            if cur_s is not None:
                out.append(Chunk(cur_s, cur_e, block.page, block.section, block.atomic))
                cur_s = None
            out.append(Chunk(s, cut, block.page, block.section, block.atomic))
            s = cut + 1 if text[cut : cut + 1] == " " else cut
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= target:
            cur_e = e
        else:
            out.append(Chunk(cur_s, cur_e, block.page, block.section, block.atomic))
            cur_s, cur_e = s, e
    if cur_s is not None and cur_e > cur_s:
        out.append(Chunk(cur_s, cur_e, block.page, block.section, block.atomic))
    return out


def chunk_document(doc: ParsedDocument, target: int = 900, hard_max: int = 1600, min_chars: int = 40) -> list[Chunk]:
    chunks: list[Chunk] = []
    for block in doc.blocks:
        text = doc.units[block.page]
        length = block.end - block.start
        if length > hard_max and not block.is_code:
            chunks.extend(_split_large(text, block, target, hard_max))
            continue
        prev = chunks[-1] if chunks else None
        can_merge = (
            prev is not None
            and not block.atomic and not prev.atomic and not block.is_code
            and prev.page == block.page and prev.section == block.section
            and (block.end - prev.start) <= target
        )
        if can_merge:
            prev.end = block.end
        else:
            chunks.append(Chunk(block.start, block.end, block.page, block.section, block.atomic))

    # Absorb fragments that are too small to stand alone into a same-section neighbour.
    merged: list[Chunk] = []
    for ch in chunks:
        size = len(doc.units[ch.page][ch.start : ch.end].strip())
        prev = merged[-1] if merged else None
        if (
            size < min_chars and not ch.atomic and prev is not None and not prev.atomic
            and prev.page == ch.page and prev.section == ch.section
            and ch.end - prev.start <= hard_max
        ):
            prev.end = ch.end
            continue
        merged.append(ch)
    return merged
