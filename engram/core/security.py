"""Security helpers: file validation, safe names and path containment.

Imported documents are *untrusted data*. They are never executed, never interpreted
as instructions, and are stored content-addressed under the data directory so a
user-supplied filename can never choose where bytes land on disk.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path


class SecurityError(ValueError):
    """Raised when an input violates a safety rule."""


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, max_len: int = 120) -> str:
    """Reduce an arbitrary (possibly hostile) filename to a harmless display name."""
    name = unicodedata.normalize("NFKC", name or "")
    # Drop any directory component, for both separators, regardless of platform.
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "")
    name = _SAFE_NAME.sub("_", name).strip(" .")
    stem = name.split(".")[0].upper()
    if not name or stem in _WINDOWS_RESERVED:
        name = f"file_{name or 'unnamed'}"
    if len(name) > max_len:
        suffix = Path(name).suffix[:10]
        name = name[: max_len - len(suffix)] + suffix
    return name


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and guarantee it stays inside ``base`` (no traversal)."""
    base_r = base.resolve()
    cand_r = candidate.resolve()
    try:
        cand_r.relative_to(base_r)
    except ValueError as exc:
        raise SecurityError(f"path escapes the allowed directory: {candidate}") from exc
    return cand_r


def detect_kind(filename: str, head: bytes, allowed_extensions: tuple[str, ...]) -> str:
    """Decide how to parse a file from its extension *and* its content.

    Returns one of ``pdf``, ``markdown`` or ``text``. Raises ``SecurityError`` for
    disallowed extensions or for content that does not match its extension.
    """
    ext = Path(filename).suffix.lower()
    if ext not in allowed_extensions:
        raise SecurityError(f"file type not allowed: {ext or '(none)'}")
    if ext == ".pdf":
        if not head.lstrip()[:5].startswith(b"%PDF-"):
            raise SecurityError("file has .pdf extension but is not a PDF")
        return "pdf"
    if head.lstrip()[:5].startswith(b"%PDF-"):
        raise SecurityError("binary PDF content with a text extension")
    if _looks_binary(head):
        raise SecurityError("file appears to be binary, not text")
    return "markdown" if ext in (".md", ".markdown") else "text"


def _looks_binary(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 BOM: text, decoded later
        return False
    if b"\x00" in head:
        return True
    # Known executable / archive signatures.
    for magic in (b"MZ", b"\x7fELF", b"PK\x03\x04", b"\xca\xfe\xba\xbe", b"\x89PNG", b"\xff\xd8\xff", b"GIF8"):
        if head.startswith(magic):
            return True
    control = sum(1 for b in head if b < 9 or 13 < b < 32)
    return control / len(head) > 0.05


def decode_text(data: bytes) -> str:
    """Decode text bytes robustly without guessing silently: utf-8 first, then utf-16, then cp1252/latin-1."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    """Normalise extracted text: strip control chars, unify newlines. Content is otherwise kept verbatim."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)
