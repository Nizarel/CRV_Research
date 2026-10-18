"""In-memory evidence, embedding-cache and projection repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from ...domain.errors import IntegrityError
from ...domain.projection import ProjectionItem, ProjectionState
from ...domain.vectors import Vector
from ...ports.evidence import (
    CachedExtraction,
    ChunkRecord,
    EvidenceRecord,
    EvidenceStatus,
    FigureAsset,
    FigureState,
)


class InMemoryEvidenceStore:
    """Implements the evidence, embedding-cache, extraction-cache and projection ports."""

    def __init__(self) -> None:
        self.documents: dict[str, EvidenceRecord] = {}
        self.chunks: dict[str, ChunkRecord] = {}
        self.figure_assets: dict[tuple[str, str], FigureAsset] = {}
        self.vectors: dict[tuple[str, str, int], Vector] = {}
        self.projections: dict[tuple[str, str, str], ProjectionItem] = {}
        self.extractions: dict[str, CachedExtraction] = {}

    # --- ExtractionCache -----------------------------------------------------------------

    async def cached_extraction(self, fingerprint: str) -> CachedExtraction | None:
        return self.extractions.get(fingerprint)

    async def cache_extraction(self, entry: CachedExtraction, *, now: datetime) -> None:
        self.extractions.setdefault(entry.fingerprint, entry)

    # --- EvidenceRepository -------------------------------------------------------------

    async def record_document(
        self, record: EvidenceRecord, chunks: Sequence[ChunkRecord]
    ) -> EvidenceRecord:
        existing = self.documents.get(record.evidence_id)
        if existing is not None:
            if existing.chunk_set.sha256 != record.chunk_set.sha256:
                raise IntegrityError(f"Evidence {record.evidence_id} has a different chunk set")
            return existing
        self.documents[record.evidence_id] = record
        for chunk in chunks:
            self.chunks.setdefault(chunk.chunk_id, chunk)
        return record

    async def get_document(self, evidence_id: str) -> EvidenceRecord | None:
        return self.documents.get(evidence_id)

    async def latest_document(
        self, content_hash: str, policy_version: str, *, status: EvidenceStatus
    ) -> EvidenceRecord | None:
        candidates = [
            record
            for record in self.documents.values()
            if record.content_hash == content_hash
            and record.policy_version == policy_version
            and record.status is status
        ]
        return max(candidates, key=lambda r: (r.updated_at, r.evidence_id), default=None)

    async def mark_embedded(self, evidence_id: str, *, now: datetime) -> None:
        record = self.documents[evidence_id]
        if record.status is not EvidenceStatus.EMBEDDED:
            self.documents[evidence_id] = record.model_copy(
                update={"status": EvidenceStatus.EMBEDDED, "updated_at": now}
            )

    async def record_figures(self, assets: Sequence[FigureAsset], *, now: datetime) -> None:
        for asset in assets:
            key = (asset.unit_id, asset.figure_id)
            current = self.figure_assets.get(key)
            if current is not None and current.state is FigureState.STORED:
                continue
            self.figure_assets[key] = asset

    async def figures(self, unit_ids: Sequence[str]) -> list[FigureAsset]:
        wanted = set(unit_ids)
        return sorted(
            (asset for (unit, _), asset in self.figure_assets.items() if unit in wanted),
            key=lambda asset: (asset.unit_id, asset.figure_id),
        )

    # --- EmbeddingCache ------------------------------------------------------------------

    async def get_many(
        self, keys: Sequence[str], *, model: str, dimensions: int
    ) -> dict[str, Vector]:
        found: dict[str, Vector] = {}
        for key in keys:
            vector = self.vectors.get((key, model, dimensions))
            if vector is not None:
                found[key] = vector
        return found

    async def put_many(
        self, vectors: Mapping[str, Vector], *, model: str, dimensions: int, now: datetime
    ) -> None:
        for key, vector in vectors.items():
            if len(vector) != dimensions:
                raise IntegrityError(f"Vector for {key} has {len(vector)} dimensions")
            self.vectors.setdefault((key, model, dimensions), vector)

    # --- ProjectionRepository ------------------------------------------------------------

    async def upsert(self, items: Sequence[ProjectionItem], *, now: datetime) -> None:
        for item in items:
            key = (item.store, item.target, item.projection_id)
            current = self.projections.get(key)
            if current is None:
                self.projections[key] = item
            elif current.payload_sha256 != item.payload_sha256 or current.state in (
                ProjectionState.RETIRED,
                ProjectionState.FAILED_PERMANENT,
            ):
                self.projections[key] = item.model_copy(
                    update={"state": ProjectionState.PENDING, "attempts": current.attempts}
                )

    async def items(self, store: str, target: str, keys: Sequence[str]) -> list[ProjectionItem]:
        return [
            self.projections[(store, target, key)]
            for key in keys
            if (store, target, key) in self.projections
        ]

    async def active_for_content(
        self, store: str, target: str, content_hash: str
    ) -> list[ProjectionItem]:
        return [
            item
            for (s, t, _), item in self.projections.items()
            if (s, t) == (store, target)
            and item.content_hash == content_hash
            and item.state is not ProjectionState.RETIRED
        ]

    async def set_state(
        self,
        store: str,
        target: str,
        states: Mapping[str, tuple[ProjectionState, str | None]],
        *,
        now: datetime,
        count_attempt: bool = False,
    ) -> None:
        for key, (state, error) in states.items():
            item = self.projections[(store, target, key)]
            self.projections[(store, target, key)] = item.model_copy(
                update={
                    "state": state,
                    "last_error": error,
                    "attempts": item.attempts + (1 if count_attempt else 0),
                }
            )
