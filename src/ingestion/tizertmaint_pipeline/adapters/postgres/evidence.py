"""PostgreSQL evidence, embedding-cache and projection repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ...domain.errors import IntegrityError
from ...domain.ids import sha256_hex
from ...domain.models import ArtifactRef
from ...domain.projection import ProjectionItem, ProjectionState
from ...domain.vectors import VECTOR_ENCODING, Vector, decode_f32le, encode_f32le
from ...ports.evidence import (
    CachedExtraction,
    ChunkRecord,
    EvidenceRecord,
    EvidenceStatus,
    FigureAsset,
    FigureState,
)
from .pool import PgPool

_DOCUMENT_COLUMNS = (
    "evidence_id, plan_id, content_hash, policy_version, completeness, status, manifest,"
    " chunk_set, chunk_count, created_at, updated_at"
)
_INSERT_DOCUMENT = """
INSERT INTO ingest.evidence_document (evidence_id, plan_id, content_hash, policy_version,
    completeness, status, manifest, chunk_set, chunk_count, created_at, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11)
ON CONFLICT (evidence_id) DO NOTHING
"""
_INSERT_CHUNK = """
INSERT INTO ingest.chunk (chunk_id, evidence_id, ordinal, kind, chunker, tokenizer, text_sha256,
    token_count, original_pages, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::int[], $10)
ON CONFLICT (chunk_id) DO NOTHING
"""
_UPSERT_FIGURE = """
INSERT INTO ingest.figure_asset (unit_id, figure_id, state, artifact, reason, updated_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6)
ON CONFLICT (unit_id, figure_id) DO UPDATE
SET state = EXCLUDED.state, artifact = EXCLUDED.artifact, reason = EXCLUDED.reason,
    updated_at = EXCLUDED.updated_at
WHERE ingest.figure_asset.state <> 'stored'
"""
_INSERT_VECTOR = """
INSERT INTO ingest.embedding (input_sha256, model, dimensions, encoding, vector, vector_sha256,
    created_at)
SELECT key, $2, $3, $4, vector, digest, $7
FROM unnest($1::text[], $5::bytea[], $6::text[]) AS item(key, vector, digest)
ON CONFLICT (input_sha256, model, dimensions) DO NOTHING
"""
_PROJECTION_COLUMNS = (
    "store, target, projection_id, release_id, evidence_id, chunk_id, content_hash, placement_id,"
    " payload_sha256, state, attempts, last_error"
)
_UPSERT_PROJECTION = """
INSERT INTO ingest.projection_item (store, target, projection_id, release_id, evidence_id,
    chunk_id, content_hash, placement_id, payload_sha256, state, attempts, created_at,
    updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending', 0, $10, $10)
ON CONFLICT (store, target, projection_id) DO UPDATE
SET payload_sha256 = EXCLUDED.payload_sha256, state = 'pending', last_error = NULL,
    release_id = EXCLUDED.release_id, evidence_id = EXCLUDED.evidence_id,
    chunk_id = EXCLUDED.chunk_id, content_hash = EXCLUDED.content_hash,
    placement_id = EXCLUDED.placement_id, updated_at = EXCLUDED.updated_at
WHERE ingest.projection_item.payload_sha256 <> EXCLUDED.payload_sha256
   OR ingest.projection_item.state IN ('retired', 'failed_permanent')
"""


_INSERT_EXTRACTION = """
INSERT INTO ingest.extraction_cache (fingerprint, provider, content_hash, raw, page_map, details,
    created_at)
VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7)
ON CONFLICT (fingerprint) DO NOTHING
"""


def _artifact(value: Any) -> ArtifactRef:
    return ArtifactRef.model_validate(value)


def _document(row: Any) -> EvidenceRecord:
    values = dict(row)
    values["manifest"] = _artifact(values["manifest"])
    values["chunk_set"] = _artifact(values["chunk_set"])
    return EvidenceRecord.model_validate(values)


class PostgresEvidenceStore:
    """Implements the evidence, embedding-cache, extraction-cache and projection ports."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def cached_extraction(self, fingerprint: str) -> CachedExtraction | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT fingerprint, provider, content_hash, raw, page_map, details"
                " FROM ingest.extraction_cache WHERE fingerprint = $1",
                fingerprint,
            )
        return None if row is None else CachedExtraction.model_validate(dict(row))

    async def cache_extraction(self, entry: CachedExtraction, *, now: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                _INSERT_EXTRACTION,
                entry.fingerprint,
                entry.provider,
                entry.content_hash,
                entry.raw.model_dump(mode="json"),
                entry.page_map,
                entry.details,
                now,
            )

    async def record_document(
        self, record: EvidenceRecord, chunks: Sequence[ChunkRecord]
    ) -> EvidenceRecord:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                _INSERT_DOCUMENT,
                record.evidence_id,
                record.plan_id,
                record.content_hash,
                record.policy_version,
                record.completeness,
                record.status.value,
                record.manifest.model_dump(mode="json"),
                record.chunk_set.model_dump(mode="json"),
                record.chunk_count,
                record.created_at,
                record.updated_at,
            )
            for chunk in chunks:
                await connection.execute(
                    _INSERT_CHUNK,
                    chunk.chunk_id,
                    chunk.evidence_id,
                    chunk.ordinal,
                    chunk.kind,
                    chunk.chunker,
                    chunk.tokenizer,
                    chunk.text_sha256,
                    chunk.token_count,
                    list(chunk.pages),
                    record.created_at,
                )
            row = await connection.fetchrow(
                f"SELECT {_DOCUMENT_COLUMNS} FROM ingest.evidence_document WHERE evidence_id = $1",
                record.evidence_id,
            )
        stored = _document(row)
        if stored.chunk_set.sha256 != record.chunk_set.sha256:
            raise IntegrityError(f"Evidence {record.evidence_id} has a different chunk set")
        return stored

    async def get_document(self, evidence_id: str) -> EvidenceRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_DOCUMENT_COLUMNS} FROM ingest.evidence_document WHERE evidence_id = $1",
                evidence_id,
            )
        return None if row is None else _document(row)

    async def latest_document(
        self, content_hash: str, policy_version: str, *, status: EvidenceStatus
    ) -> EvidenceRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_DOCUMENT_COLUMNS} FROM ingest.evidence_document"
                " WHERE content_hash = $1 AND policy_version = $2 AND status = $3"
                " ORDER BY updated_at DESC, evidence_id DESC LIMIT 1",
                content_hash,
                policy_version,
                status.value,
            )
        return None if row is None else _document(row)

    async def mark_embedded(self, evidence_id: str, *, now: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE ingest.evidence_document SET status = 'embedded', updated_at = $2"
                " WHERE evidence_id = $1 AND status <> 'embedded'",
                evidence_id,
                now,
            )

    async def record_figures(self, assets: Sequence[FigureAsset], *, now: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            for asset in assets:
                await connection.execute(
                    _UPSERT_FIGURE,
                    asset.unit_id,
                    asset.figure_id,
                    asset.state.value,
                    asset.artifact.model_dump(mode="json") if asset.artifact else None,
                    asset.reason,
                    now,
                )

    async def figures(self, unit_ids: Sequence[str]) -> list[FigureAsset]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT unit_id, figure_id, state, artifact, reason FROM ingest.figure_asset"
                " WHERE unit_id = ANY($1::text[]) ORDER BY unit_id, figure_id",
                list(unit_ids),
            )
        return [
            FigureAsset(
                unit_id=row["unit_id"],
                figure_id=row["figure_id"],
                state=FigureState(row["state"]),
                artifact=_artifact(row["artifact"]) if row["artifact"] else None,
                reason=row["reason"],
            )
            for row in rows
        ]

    async def get_many(
        self, keys: Sequence[str], *, model: str, dimensions: int
    ) -> dict[str, Vector]:
        if not keys:
            return {}
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT input_sha256, vector, vector_sha256 FROM ingest.embedding"
                " WHERE input_sha256 = ANY($1::text[]) AND model = $2 AND dimensions = $3",
                list(keys),
                model,
                dimensions,
            )
        found: dict[str, Vector] = {}
        for row in rows:
            data = bytes(row["vector"])
            if sha256_hex(data) != row["vector_sha256"]:
                raise IntegrityError(f"Cached vector {row['input_sha256']} failed verification")
            found[row["input_sha256"]] = decode_f32le(data, dimensions=dimensions)
        return found

    async def put_many(
        self, vectors: Mapping[str, Vector], *, model: str, dimensions: int, now: datetime
    ) -> None:
        if not vectors:
            return
        keys = list(vectors)
        encoded = [encode_f32le(vectors[key]) for key in keys]
        if any(len(data) != 4 * dimensions for data in encoded):
            raise IntegrityError("Vector dimensions do not match the cache key")
        async with self._pool.acquire() as connection:
            await connection.execute(
                _INSERT_VECTOR,
                keys,
                model,
                dimensions,
                VECTOR_ENCODING,
                encoded,
                [sha256_hex(data) for data in encoded],
                now,
            )

    async def upsert(self, items: Sequence[ProjectionItem], *, now: datetime) -> None:
        # Key order: copies of one content project the same rows from concurrent jobs.
        ordered = sorted(items, key=lambda item: (item.store, item.target, item.projection_id))
        async with self._pool.acquire() as connection, connection.transaction():
            for item in ordered:
                await connection.execute(
                    _UPSERT_PROJECTION,
                    item.store,
                    item.target,
                    item.projection_id,
                    item.release_id,
                    item.evidence_id,
                    item.chunk_id,
                    item.content_hash,
                    item.placement_id,
                    item.payload_sha256,
                    now,
                )

    async def items(self, store: str, target: str, keys: Sequence[str]) -> list[ProjectionItem]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {_PROJECTION_COLUMNS} FROM ingest.projection_item"
                " WHERE store = $1 AND target = $2 AND projection_id = ANY($3::text[])",
                store,
                target,
                list(keys),
            )
        return [ProjectionItem.model_validate(dict(row)) for row in rows]

    async def active_for_content(
        self, store: str, target: str, content_hash: str
    ) -> list[ProjectionItem]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {_PROJECTION_COLUMNS} FROM ingest.projection_item"
                " WHERE store = $1 AND target = $2 AND content_hash = $3"
                " AND state <> 'retired'",
                store,
                target,
                content_hash,
            )
        return [ProjectionItem.model_validate(dict(row)) for row in rows]

    async def set_state(
        self,
        store: str,
        target: str,
        states: Mapping[str, tuple[ProjectionState, str | None]],
        *,
        now: datetime,
        count_attempt: bool = False,
    ) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            for key in sorted(states):
                state, error = states[key]
                await connection.execute(
                    "UPDATE ingest.projection_item SET state = $4, last_error = $5,"
                    " attempts = attempts + $6, updated_at = $7"
                    " WHERE store = $1 AND target = $2 AND projection_id = $3",
                    store,
                    target,
                    key,
                    state.value,
                    error[:2000] if error else None,
                    1 if count_attempt else 0,
                    now,
                )
