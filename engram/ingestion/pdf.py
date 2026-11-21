"""PDF parser (pypdf, pure Python). Extracts text per page; never executes embedded content.

Scanned PDFs without a text layer produce no text: that is reported as a warning
rather than silently "succeeding" with an empty memory (OCR is on the roadmap).
"""
from __future__ import annotations

import io
import logging
import re

from engram.core.security import SecurityError, clean_text
from engram.ingestion.metadata import pdf_date
from engram.ingestion.parsed import Block, ParsedDocument
from engram.ingestion.text import paragraph_spans, split_labelled_lines

log = logging.getLogger("engram.pdf")
logging.getLogger("pypdf").setLevel(logging.ERROR)


def _normalise_page(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"-\n(?=[a-záéíóúñ])", "", text)          # re-join hyphenated words
    text = re.sub(r"[ \t]+\n", "\n", text)
    # Lines that wrap mid-sentence become one line; real paragraph breaks are kept.
    text = re.sub(r"(?<=[^\n.!?:;])\n(?=[a-záéíóúñ(])", " ", text)
    return text.strip()


def parse_pdf(data: bytes, fallback_title: str, max_pages: int = 2000) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise SecurityError("encrypted PDF (password required)") from exc
        pages = reader.pages
    except SecurityError:
        raise
    except Exception as exc:
        raise SecurityError(f"unreadable PDF: {exc}") from exc

    units: dict[int | None, str] = {}
    blocks: list[Block] = []
    warnings: list[str] = []
    n_pages = len(pages)
    if n_pages > max_pages:
        warnings.append(f"only the first {max_pages} of {n_pages} pages were processed")
    for i, page in enumerate(pages[:max_pages], start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # malformed page: keep going, report it
            warnings.append(f"page {i}: text extraction failed ({exc.__class__.__name__})")
            raw = ""
        text = _normalise_page(raw)
        units[i] = text
        for s, e in paragraph_spans(text):
            for ss, ee, atomic in split_labelled_lines(text, s, e):
                blocks.append(Block(start=ss, end=ee, page=i, atomic=atomic))
    if not blocks:
        warnings.append("no extractable text (scanned PDF? OCR is not supported yet)")

    meta = {}
    info = {}
    try:
        info = reader.metadata or {}
    except Exception:
        pass
    for key in ("/Title", "/Author", "/Subject", "/Creator", "/Producer", "/CreationDate", "/ModDate"):
        if key in info and info[key]:
            meta[key.strip("/").lower()] = str(info[key])[:300]
    meta["pages"] = n_pages

    title = (meta.get("title") or "").strip() or fallback_title
    authored = pdf_date(meta.get("creationdate"))
    return ParsedDocument(
        title=title,
        kind="pdf",
        units=units,
        blocks=blocks,
        metadata={"pdf": meta},
        authored_at=authored,
        authored_at_source=f"PDF metadata CreationDate '{meta.get('creationdate')}'" if authored else None,
        warnings=warnings,
    )
