"""Tiny dependency-free PDF writer used by tests and scripts/make_demo_pdf.py (text only, Helvetica)."""
from __future__ import annotations

import textwrap


def _esc(s: str) -> bytes:
    raw = s.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(pages: list[list[str]], title: str = "", creation_date: str | None = None, width: int = 90) -> bytes:
    """``pages`` is a list of pages, each a list of paragraphs. ``creation_date`` like '20240301'."""
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3
    page_ids = [4 + i * 2 for i in range(n_pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, paras in enumerate(pages):
        lines: list[str] = []
        for p in paras:
            lines += textwrap.wrap(p, width) or [""]
            lines.append("")
        stream = b"BT /F1 11 Tf 14 TL 50 790 Td\n"
        for line in lines:
            stream += b"(" + _esc(line) + b") '\n"
        stream += b"ET"
        content_id = page_ids[i] + 1
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    info = b"<< /Producer (engram-tests)"
    if title:
        info += b" /Title (" + _esc(title) + b")"
    if creation_date:
        info += f" /CreationDate (D:{creation_date}000000)".encode()
    info += b" >>"
    objs.append(info)
    info_id = len(objs)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
