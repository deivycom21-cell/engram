"""Domain model: the Engram (unit of knowledge) and its relations."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EngramKind(StrEnum):
    FACT = "fact"
    IDEA = "idea"
    EVENT = "event"
    DECISION = "decision"
    CONCEPT = "concept"
    OBSERVATION = "observation"
    QUESTION = "question"
    PROJECT = "project"


class RelationOrigin(StrEnum):
    EXPLICIT = "explicit"      # written in the source (wiki-link, "see X", explicit reference)
    STRUCTURAL = "structural"  # follows from document structure (order inside a document)
    INFERRED = "inferred"      # computed by ENGRAM; always a proposal with an explanation


class RelationType(StrEnum):
    FOLLOWS = "follows"                          # structural: next fragment in the same document
    REFERENCES = "references"                    # explicit: source links to target
    SEMANTICALLY_SIMILAR = "semantically_similar"  # inferred: vector similarity (NOT a factual link)
    SHARES_CONCEPTS = "shares_concepts"          # inferred: overlapping rare concepts/entities
    POSSIBLE_DUPLICATE = "possible_duplicate"    # inferred: near-identical content
    POSSIBLE_CONTRADICTION = "possible_contradiction"  # inferred: same topic, opposing polarity/values
    LATER_VERSION = "later_version"              # inferred: same topic, newer document/date (evolution)


class RelationStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_uid(prefix: str) -> str:
    """Time-sortable unique id (ULID layout: 48-bit ms timestamp + 80 random bits)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


class Provenance(BaseModel):
    document_id: int | None
    document_uid: str | None = None
    document_title: str | None = None
    filename: str | None = None
    sha256: str | None = None
    page: int | None = None
    section: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    position: int | None = None


class Engram(BaseModel):
    id: int
    uid: str
    kind: EngramKind
    kind_confidence: float | None = None
    kind_reason: str | None = None
    title: str | None = None
    content: str
    content_hash: str
    occurred_at: str | None = None
    occurred_at_source: str | None = None
    created_at: str | None = None
    created_at_source: str | None = None
    ingested_at: str
    confidence: float | None = None
    status: str = "active"
    access_count: int = 0
    last_accessed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    concepts: list[dict[str, Any]] = Field(default_factory=list)
    has_embedding: bool = False

    @property
    def effective_date(self) -> str | None:
        return self.occurred_at or self.created_at

    @classmethod
    def from_row(cls, row, concepts=None, has_embedding=False) -> "Engram":
        keys = row.keys()
        g = lambda k: row[k] if k in keys else None  # noqa: E731
        return cls(
            id=row["id"],
            uid=row["uid"],
            kind=EngramKind(row["kind"]),
            kind_confidence=row["kind_confidence"],
            kind_reason=row["kind_reason"],
            title=row["title"],
            content=row["content"],
            content_hash=row["content_hash"],
            occurred_at=row["occurred_at"],
            occurred_at_source=row["occurred_at_source"],
            created_at=row["created_at"],
            created_at_source=row["created_at_source"],
            ingested_at=row["ingested_at"],
            confidence=row["confidence"],
            status=row["status"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            metadata=json.loads(row["metadata"] or "{}"),
            provenance=Provenance(
                document_id=row["document_id"],
                document_uid=g("doc_uid"),
                document_title=g("doc_title"),
                filename=g("doc_filename"),
                sha256=g("doc_sha256"),
                page=row["page"],
                section=row["title"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                position=row["position"],
            ),
            concepts=concepts or [],
            has_embedding=has_embedding,
        )


class Relation(BaseModel):
    id: int
    source_id: int
    target_id: int
    type: RelationType
    origin: RelationOrigin
    explanation: str
    confidence: float
    evidence: dict[str, Any] = Field(default_factory=dict)
    detector: str
    status: RelationStatus
    created_at: str

    @classmethod
    def from_row(cls, row) -> "Relation":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            type=RelationType(row["type"]),
            origin=RelationOrigin(row["origin"]),
            explanation=row["explanation"],
            confidence=row["confidence"],
            evidence=json.loads(row["evidence"] or "{}"),
            detector=row["detector"],
            status=RelationStatus(row["status"]),
            created_at=row["created_at"],
        )


class Document(BaseModel):
    id: int
    uid: str
    title: str
    filename: str
    original_path: str | None
    stored_path: str
    sha256: str
    kind: str
    size_bytes: int
    authored_at: str | None
    authored_at_source: str | None
    file_modified_at: str | None
    ingested_at: str
    previous_version_id: int | None
    metadata: dict[str, Any]
    status: str
    engram_count: int = 0

    @classmethod
    def from_row(cls, row) -> "Document":
        data = dict(row)
        data["metadata"] = json.loads(data.get("metadata") or "{}")
        data.setdefault("engram_count", 0)
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
