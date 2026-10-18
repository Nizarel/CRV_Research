"""Evidence, embedding-cache and projection repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..domain.models import ArtifactRef
from ..domain.projection import ProjectionItem, ProjectionState
from ..domain.vectors import Vector


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceStatus(StrEnum):
    CHUNKED = "chunked"
    EMBEDDED = "embedded"


class EvidenceRecord(_Frozen):
    evidence_id: str
    plan_id: str
    content_hash: str
    policy_version: str
    completeness: str
    status: EvidenceStatus
    manifest: ArtifactRef
    chunk_set: ArtifactRef
    chunk_count: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ChunkRecord(_Frozen):
    chunk_id: str
    evidence_id: str
    ordinal: int = Field(ge=0)
    kind: str
    chunker: str
    tokenizer: str
    text_sha256: str
    token_count: int = Field(gt=0)
    pages: tuple[int, ...] = ()


class FigureState(StrEnum):
    STORED = "stored"
    FAILED = "failed"
    EXPIRED = "expired"


class FigureAsset(_Frozen):
    unit_id: str
    figure_id: str
    state: FigureState
    artifact: ArtifactRef | None = None
    reason: str | None = None


class EvidenceRepository(Protocol):
    async def record_document(
        self, record: EvidenceRecord, chunks: Sequence[ChunkRecord]
    ) -> EvidenceRecord:
        """Insert idempotently by ``evidence_id``; return the stored record."""
        ...

    async def get_document(self, evidence_id: str) -> EvidenceRecord | None: ...

    async def latest_document(
        self, content_hash: str, policy_version: str, *, status: EvidenceStatus
    ) -> EvidenceRecord | None: ...

    async def mark_embedded(self, evidence_id: str, *, now: datetime) -> None: ...

    async def record_figures(self, assets: Sequence[FigureAsset], *, now: datetime) -> None:
        """Upsert; a stored asset is never downgraded."""
        ...

    async def figures(self, unit_ids: Sequence[str]) -> list[FigureAsset]: ...


class EmbeddingCache(Protocol):
    async def get_many(
        self, keys: Sequence[str], *, model: str, dimensions: int
    ) -> dict[str, Vector]: ...

    async def put_many(
        self, vectors: Mapping[str, Vector], *, model: str, dimensions: int, now: datetime
    ) -> None:
        """Insert missing keys; an existing vector for a key is kept (first writer wins)."""
        ...


class CachedExtraction(_Frozen):
    """A stored paid analysis result, keyed by everything that shaped it."""

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str
    content_hash: str
    raw: ArtifactRef
    page_map: dict[str, int] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExtractionCache(Protocol):
    async def cached_extraction(self, fingerprint: str) -> CachedExtraction | None: ...

    async def cache_extraction(self, entry: CachedExtraction, *, now: datetime) -> None:
        """Insert; an existing entry for the fingerprint is kept (first writer wins)."""
        ...


class ProjectionRepository(Protocol):
    async def upsert(self, items: Sequence[ProjectionItem], *, now: datetime) -> None:
        """Insert new keys; a changed payload digest resets the key to ``pending``."""
        ...

    async def items(self, store: str, target: str, keys: Sequence[str]) -> list[ProjectionItem]: ...

    async def active_for_content(
        self, store: str, target: str, content_hash: str
    ) -> list[ProjectionItem]:
        """Every non-retired item of the content (its row may or may not exist yet)."""
        ...

    async def set_state(
        self,
        store: str,
        target: str,
        states: Mapping[str, tuple[ProjectionState, str | None]],
        *,
        now: datetime,
        count_attempt: bool = False,
    ) -> None: ...
