"""Intermediate representation produced by every parser."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    """A contiguous, verbatim span of a document's extracted text."""

    start: int                 # offset into the unit text (page text for PDFs, full text otherwise)
    end: int
    page: int | None = None    # 1-based page number (PDF only)
    section: str | None = None  # heading path, e.g. "Architecture › Storage"
    atomic: bool = False       # explicitly labelled item ("Decision: ...") -> never merged with neighbours
    is_code: bool = False


@dataclass
class ParsedDocument:
    title: str
    kind: str                                  # pdf | markdown | text
    units: dict[int | None, str]               # page number (or None) -> extracted text
    blocks: list[Block]
    metadata: dict = field(default_factory=dict)
    authored_at: str | None = None
    authored_at_source: str | None = None
    default_kind: str | None = None            # document-level "type:" from front-matter
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # explicit [[wiki links]] / references
    warnings: list[str] = field(default_factory=list)

    def text_of(self, block: Block) -> str:
        return self.units[block.page][block.start : block.end]
