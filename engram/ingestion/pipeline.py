"""Ingestion pipeline: validate → store original → extract → chunk → classify → embed → persist → associate.

* Files are identified by SHA-256: importing the same bytes twice is a no-op.
* The same filename with different bytes becomes a *new version* (``previous_version_id``);
  the old version and its engrams are kept, so the evolution of a note stays visible.
* Originals are copied, content-addressed, into ``<data>/sources`` so every engram can
  be traced back to the exact bytes it came from even if the user's file later changes.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from engram.core.security import SecurityError, clean_text, decode_text, detect_kind, ensure_within, safe_filename
from engram.core.textutil import concept_key, norm
from engram.ingestion.chunking import chunk_document
from engram.ingestion.extraction import classify, extract
from engram.ingestion.markdown import TAG, WIKILINK, parse_markdown
from engram.ingestion.metadata import filename_date, find_dates, mtime_iso
from engram.ingestion.parsed import ParsedDocument
from engram.ingestion.pdf import parse_pdf
from engram.ingestion.text import parse_text
from engram.memory.models import RelationOrigin, RelationType

log = logging.getLogger("engram.ingestion")


@dataclass
class IngestionResult:
    filename: str
    status: str                      # ingested | duplicate | failed
    document_id: int | None = None
    engrams: int = 0
    previous_version_id: int | None = None
    associations: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class IngestionPipeline:
    def __init__(self, settings, store, vectors, provider, associations):
        self.settings = settings
        self.store = store
        self.vectors = vectors
        self.provider = provider
        self.associations = associations

    # ------------------------------------------------------------------ entry points
    def ingest_path(self, path: Path | str, recursive: bool = True) -> list[IngestionResult]:
        p = Path(path).expanduser()
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            files = sorted(f for f in p.glob(pattern) if f.is_file() and f.suffix.lower() in self.settings.allowed_extensions
                           and not any(part.startswith(".") for part in f.relative_to(p).parts))
            return [self.ingest_file(f) for f in files]
        return [self.ingest_file(p)]

    def ingest_file(self, path: Path) -> IngestionResult:
        name = path.name
        try:
            if path.is_symlink():
                raise SecurityError("symbolic links are not followed")
            if not path.is_file():
                raise SecurityError("not a regular file")
            size = path.stat().st_size
            if size > self.settings.max_file_bytes:
                raise SecurityError(f"file too large ({size / 1e6:.1f} MB > {self.settings.max_file_bytes / 1e6:.0f} MB)")
            data = path.read_bytes()
            return self.ingest_bytes(data, name, original_path=str(path.resolve()), mtime=path.stat().st_mtime)
        except SecurityError as exc:
            return IngestionResult(safe_filename(name), "failed", error=str(exc))
        except OSError as exc:
            return IngestionResult(safe_filename(name), "failed", error=f"cannot read file: {exc.strerror or exc}")

    def ingest_bytes(self, data: bytes, filename: str, original_path: str | None = None,
                     mtime: float | None = None) -> IngestionResult:
        name = safe_filename(filename)
        try:
            return self._ingest(data, name, original_path, mtime)
        except SecurityError as exc:
            return IngestionResult(name, "failed", error=str(exc))
        except Exception as exc:  # never hide errors: report them in the result and the log
            log.exception("ingestion failed for %s", name)
            return IngestionResult(name, "failed", error=f"{exc.__class__.__name__}: {exc}")

    # ------------------------------------------------------------------ core
    def _ingest(self, data: bytes, name: str, original_path: str | None, mtime: float | None) -> IngestionResult:
        s = self.settings
        if len(data) > s.max_file_bytes:
            raise SecurityError(f"file too large ({len(data) / 1e6:.1f} MB)")
        if not data.strip():
            raise SecurityError("empty file")
        kind = detect_kind(name, data[:4096], s.allowed_extensions)
        sha = hashlib.sha256(data).hexdigest()
        existing = self.store.document_by_sha(sha)
        if existing:
            return IngestionResult(name, "duplicate", document_id=existing.id,
                                   warnings=[f"identical content already imported as '{existing.filename}' (document {existing.id})"])

        fallback_title = Path(name).stem.replace("_", " ").strip() or name
        if kind == "pdf":
            parsed = parse_pdf(data, fallback_title, s.max_pdf_pages)
        else:
            text = clean_text(decode_text(data))
            parsed = parse_markdown(text, fallback_title) if kind == "markdown" else parse_text(text, fallback_title)

        authored, authored_src = parsed.authored_at, parsed.authored_at_source
        if not authored and (fd := filename_date(name)):
            authored, authored_src = fd, f"filename '{name}'"
        if not authored:
            # Only a date in the title line counts as the document's date; a date in the first
            # paragraph belongs to that entry, not to the whole document.
            title_line = parsed.title if parsed.title != fallback_title else ""
            header = find_dates(title_line, 1)
            if header:
                authored, authored_src = header[0].value, f"document title {header[0].source}"

        chunks = chunk_document(parsed, s.chunk_target_chars, s.chunk_max_chars, s.chunk_min_chars)
        records = []
        for ch in chunks:
            text = parsed.units[ch.page][ch.start : ch.end]
            if not text.strip():
                continue
            occurred, occurred_src = None, None
            dates = find_dates(text, 1)
            if dates:
                occurred, occurred_src = dates[0].value, dates[0].source
            elif ch.section:
                sec_dates = find_dates(ch.section, 1)
                if sec_dates:
                    occurred, occurred_src = sec_dates[0].value, f"section heading '{ch.section}'"
            cls = classify(text, parsed.default_kind, has_date=bool(occurred))
            ext = extract(text)
            records.append({
                "chunk": ch, "text": text, "occurred": occurred, "occurred_src": occurred_src, "cls": cls, "ext": ext,
            })
        if not records:
            raise SecurityError("no text could be extracted" + (f" ({'; '.join(parsed.warnings)})" if parsed.warnings else ""))

        vectors = None
        if self.provider is not None:
            inputs = [(f"{r['chunk'].section}: " if r["chunk"].section else "") + r["text"] for r in records]
            vectors = self.provider.embed(inputs, mode="passage")

        stored_rel = Path(sha[:2]) / f"{sha}{Path(name).suffix.lower()}"
        stored_abs = ensure_within(s.sources_dir, s.sources_dir / stored_rel)
        stored_abs.parent.mkdir(parents=True, exist_ok=True)
        if not stored_abs.exists():
            tmp = stored_abs.with_suffix(stored_abs.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(stored_abs)

        previous = self.store.latest_document_by_name(name)
        meta = dict(parsed.metadata)
        if parsed.tags:
            meta["tags"] = parsed.tags
        if parsed.links:
            meta["links"] = parsed.links
        if parsed.warnings:
            meta["warnings"] = parsed.warnings
        confidence = 0.9 if kind == "pdf" else 1.0  # fidelity of stored text to the source

        new_ids: list[int] = []
        with self.store.db.transaction() as conn:
            doc_id = self.store.insert_document(
                conn, title=parsed.title[:300], filename=name, original_path=original_path,
                stored_path=stored_rel.as_posix(), sha256=sha, kind=kind, size_bytes=len(data),
                authored_at=authored, authored_at_source=authored_src, file_modified_at=mtime_iso(mtime),
                previous_version_id=previous.id if previous else None, metadata=meta,
            )
            prev_id = None
            for pos, r in enumerate(records):
                ch, cls = r["chunk"], r["cls"]
                eid = self.store.insert_engram(
                    conn, document_id=doc_id, kind=cls.kind, kind_confidence=cls.confidence, kind_reason=cls.reason,
                    title=ch.section, content=r["text"], content_hash=hashlib.sha256(norm(r["text"]).encode()).hexdigest(),
                    position=pos, page=ch.page, char_start=ch.start, char_end=ch.end,
                    occurred_at=r["occurred"], occurred_at_source=r["occurred_src"],
                    created_at=authored, created_at_source=authored_src, confidence=confidence,
                    metadata={"atomic": ch.atomic} if ch.atomic else {},
                )
                new_ids.append(eid)
                self._attach_concepts(conn, eid, r["text"], r["ext"], parsed, first=(pos == 0))
                if vectors is not None and self.vectors is not None:
                    self.vectors.add(conn, eid, vectors[pos])
                if prev_id is not None:
                    self.store.add_relation(
                        prev_id, eid, RelationType.FOLLOWS, RelationOrigin.STRUCTURAL,
                        f"Consecutive fragments of '{parsed.title[:80]}' (position {pos - 1} → {pos}).",
                        1.0, "document-structure", conn=conn,
                    )
                prev_id = eid
        if self.vectors is not None:
            self.vectors.invalidate()

        self.store.audit("document.ingested", {
            "document_id": doc_id, "filename": name, "engrams": len(new_ids), "sha256": sha,
            "previous_version_id": previous.id if previous else None,
        })
        self.resolve_links()
        assoc = self.associations.associate(new_ids).as_dict() if self.associations else {}
        if previous:
            parsed.warnings.append(f"new version of '{name}' (previous document {previous.id} kept)")
        return IngestionResult(name, "ingested", document_id=doc_id, engrams=len(new_ids),
                               previous_version_id=previous.id if previous else None,
                               associations=assoc, warnings=parsed.warnings)

    def _attach_concepts(self, conn, eid: int, text: str, ext, parsed: ParsedDocument, first: bool) -> None:
        seen: set[tuple[str, str]] = set()

        def add(name: str, kind: str, weight: float, origin: str):
            key = concept_key(name)
            if len(key) < 2 or (key, kind) in seen:
                return
            seen.add((key, kind))
            cid = self.store.upsert_concept(conn, name.strip()[:120], key, kind)
            self.store.link_concept(conn, eid, cid, weight, origin)

        for name, w in ext.entities:
            add(name, "entity", w, "extracted")
        entity_keys = {concept_key(n) for n, _ in ext.entities}
        for name, w in ext.concepts:
            if concept_key(name) not in entity_keys:  # the entity "Redis" already covers the concept "redis"
                add(name, "concept", w, "extracted")
        for tag in TAG.findall(text):
            add(tag, "tag", 2.0, "explicit")
        for link in WIKILINK.findall(text):
            add(link.strip(), "entity", 2.0, "explicit")
        if first:  # document-level tags anchor on the first fragment only
            for tag in parsed.tags:
                add(tag, "tag", 1.5, "explicit")

    # ------------------------------------------------------------------ explicit links
    def resolve_links(self) -> int:
        """Turn explicit [[wiki links]] / note links into REFERENCES relations once both ends exist."""
        created = 0
        docs = [d for d in self.store.list_documents() if d.status == "active" and d.metadata.get("links")]
        for d in docs:
            for link in d.metadata["links"]:
                target = self.store.document_by_title(Path(link).stem if link.endswith(".md") else link)
                if not target or target.id == d.id:
                    continue
                t_row = self.store.db.query_one(
                    "SELECT id FROM engrams WHERE document_id=? AND status='active' ORDER BY position LIMIT 1", (target.id,))
                s_row = self.store.db.query_one(
                    "SELECT id FROM engrams WHERE document_id=? AND status='active' AND instr(content, ?) > 0 ORDER BY position LIMIT 1",
                    (d.id, link),
                ) or self.store.db.query_one(
                    "SELECT id FROM engrams WHERE document_id=? AND status='active' ORDER BY position LIMIT 1", (d.id,))
                if not t_row or not s_row:
                    continue
                if self.store.add_relation(
                    s_row["id"], t_row["id"], RelationType.REFERENCES, RelationOrigin.EXPLICIT,
                    f"'{d.title[:80]}' explicitly links to '{target.title[:80]}' (link: {link}).",
                    1.0, "explicit-link", {"link": link, "source_document": d.id, "target_document": target.id},
                ):
                    created += 1
        return created
