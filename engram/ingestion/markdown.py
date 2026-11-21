"""Markdown parser: front-matter, heading hierarchy, paragraphs, lists, code fences, tags and wiki-links."""
from __future__ import annotations

import re

from engram.ingestion.metadata import FRONT_MATTER_DATE_KEYS, parse_front_matter, parse_loose_date
from engram.ingestion.parsed import Block, ParsedDocument
from engram.ingestion.text import split_labelled_lines

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
WIKILINK = re.compile(r"\[\[([^\[\]|#\n]{1,200})(?:#[^\[\]|\n]*)?(?:\|[^\[\]\n]*)?\]\]")
TAG = re.compile(r"(?<![\w/#&])#([A-Za-zÀ-ÿ][\w\-/À-ÿ]{1,48})")
MD_LINK_TO_NOTE = re.compile(r"\[[^\]]*\]\(([^)\s]+\.md)\)")

KIND_ALIASES = {
    "decision": "decision", "decisión": "decision", "decision-record": "decision", "adr": "decision",
    "idea": "idea", "ideas": "idea", "event": "event", "evento": "event", "meeting": "event", "reunión": "event",
    "fact": "fact", "hecho": "fact", "concept": "concept", "concepto": "concept",
    "observation": "observation", "observación": "observation", "journal": "observation", "diario": "observation",
    "question": "question", "pregunta": "question", "project": "project", "proyecto": "project",
}


def parse_markdown(text: str, fallback_title: str) -> ParsedDocument:
    fm, body_start = parse_front_matter(text)
    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []
    first_h1: str | None = None

    def section() -> str | None:
        return " › ".join(h for _, h in heading_stack) or None

    lines = text[body_start:].split("\n")
    offset = body_start
    para_start: int | None = None
    para_end = 0
    in_code = False
    code_start = 0

    def flush():
        nonlocal para_start
        if para_start is not None:
            for s, e, atomic in split_labelled_lines(text, para_start, para_end):
                blocks.append(Block(start=s, end=e, section=section(), atomic=atomic))
            para_start = None

    for line in lines:
        l_start, l_end = offset, offset + len(line)
        offset = l_end + 1
        if in_code:
            if FENCE.match(line):
                in_code = False
                blocks.append(Block(start=code_start, end=l_end, section=section(), is_code=True))
            continue
        if FENCE.match(line):
            flush()
            in_code = True
            code_start = l_start
            continue
        h = HEADING.match(line)
        if h:
            flush()
            level, title = len(h.group(1)), h.group(2).strip()
            if level == 1 and first_h1 is None:
                first_h1 = title
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue
        if not line.strip():
            flush()
            continue
        if para_start is None:
            para_start = l_start
        para_end = l_end
    if in_code:  # unterminated fence: keep the content anyway
        blocks.append(Block(start=code_start, end=len(text), section=section(), is_code=True))
    flush()

    title = str(fm.get("title") or first_h1 or fallback_title)
    tags = fm.get("tags") or fm.get("etiquetas") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
    body = text[body_start:]
    tags = list(dict.fromkeys([t.lstrip("#") for t in tags] + TAG.findall(_strip_code(body))))
    links = list(dict.fromkeys(m.strip() for m in WIKILINK.findall(body)))
    links += [l for l in MD_LINK_TO_NOTE.findall(body) if "://" not in l and l not in links]

    authored, authored_src = None, None
    for key in FRONT_MATTER_DATE_KEYS:
        if key in fm and isinstance(fm[key], str):
            parsed = parse_loose_date(fm[key])
            if parsed:
                authored, authored_src = parsed, f"front-matter '{key}: {fm[key]}'"
                break

    default_kind = None
    t = fm.get("type") or fm.get("tipo")
    if isinstance(t, str):
        default_kind = KIND_ALIASES.get(t.strip().lower())

    return ParsedDocument(
        title=title,
        kind="markdown",
        units={None: text},
        blocks=blocks,
        metadata={"front_matter": fm} if fm else {},
        authored_at=authored,
        authored_at_source=authored_src,
        default_kind=default_kind,
        tags=tags,
        links=links,
    )


def _strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"^#{1,6}\s", "", text, flags=re.M)
