"""Request schemas (responses are plain dicts built from the domain models)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from engram.memory.models import EngramKind, RelationStatus


class ReconstructRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    narrate: bool = False
    consent_external: bool = False


class EngramPatch(BaseModel):
    kind: EngramKind | None = None
    status: Literal["active", "archived"] | None = None


class RelationPatch(BaseModel):
    status: RelationStatus


class InsightPatch(BaseModel):
    status: Literal["open", "accepted", "dismissed"]
