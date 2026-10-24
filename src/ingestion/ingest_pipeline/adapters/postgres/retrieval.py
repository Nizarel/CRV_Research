"""``crv_retrieval.chunk`` (migration 0022): a ``SearchProjector`` beside Azure AI Search, the
release-copy work list and the HNSW index builds.

Rows are the Search rows built by the projection stage, so payload digests compare across the
two stores. Text search columns are derived in SQL from the same fields Search analyzes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.errors import InvalidInputError
from ...domain.projection import ProjectionReport
from ...ports.clock import Clock
from .pool import PgConnection, PgPool

STORE = "postgres"
TARGET = "crv_retrieval"
TE3_DIMENSIONS = 3072
KEY_FIELD = "projection_id"
DIGEST_FIELD = "payload_sha256"

_TEXT = "TEXT"
_TEXTS = "TEXT[]"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("projection_id", _TEXT),
    ("release_id", _TEXT),
    ("chunk_id", _TEXT),
    ("content_hash", _TEXT),
    ("chunk_kind", _TEXT),
    ("title", _TEXT),
    ("content", _TEXT),
    ("source_uri", _TEXT),
    ("source_path", _TEXT),
    ("source_paths", _TEXTS),
    ("document_code", _TEXT),
    ("identifiers", _TEXTS),
    ("original_page_numbers", "INTEGER[]"),
    ("source_locators", _TEXTS),
    ("original_source_uris", _TEXTS),
    ("original_content_hashes", _TEXTS),
    ("is_generated", "BOOLEAN"),
    ("document_class", _TEXT),
    ("citation_basis", _TEXT),
    ("extraction_route", _TEXT),
    ("language", _TEXT),
    ("payload_sha256", _TEXT),
    ("content_vector", "REAL[]"),
)
_LISTS = frozenset(name for name, kind in _COLUMNS if kind.endswith("[]"))
_REQUIRED = ("projection_id", "release_id", "chunk_id", "content_hash", "chunk_kind", "title",
             "content", "source_uri", "payload_sha256")
_UPDATED = (
    "release_id", "chunk_id", "content_hash", "chunk_kind", "title", "content", "source_uri",
    "source_path", "document_code", "identifier_keys", "original_page_numbers",
    "source_locators", "original_source_uris", "original_content_hashes", "is_generated",
    "document_class", "citation_basis", "extraction_route", "language", "payload_sha256",
    "search_text", "embedding_te3", "updated_at",
)
_UPSERT = f"""
INSERT INTO crv_retrieval.chunk (projection_id, release_id, chunk_id, content_hash, chunk_kind,
    title, content, source_uri, source_path, document_code, identifier_keys,
    original_page_numbers, source_locators, original_source_uris, original_content_hashes,
    is_generated, document_class, citation_basis, extraction_route, language, payload_sha256,
    search_text, embedding_te3, updated_at)
SELECT r.projection_id, r.release_id, r.chunk_id, r.content_hash, r.chunk_kind, r.title,
    r.content, r.source_uri, r.source_path, r.document_code,
    crv_retrieval.identifier_keys(r.identifiers || r.document_code),
    r.original_page_numbers, r.source_locators, r.original_source_uris,
    r.original_content_hashes, r.is_generated, r.document_class, r.citation_basis,
    r.extraction_route, r.language, r.payload_sha256,
    crv_retrieval.document_vector(r.title, r.identifiers || r.document_code,
        coalesce(nullif(r.source_paths, '{{}}'), ARRAY[r.source_path]), r.content, r.language),
    r.content_vector::vector({TE3_DIMENSIONS}), $2::timestamptz
FROM jsonb_to_recordset($1::jsonb) AS r({", ".join(f"{name} {kind}" for name, kind in _COLUMNS)})
ON CONFLICT (projection_id) DO UPDATE
SET {", ".join(f"{name} = EXCLUDED.{name}" for name in _UPDATED)},
    embedding_m3 = CASE WHEN crv_retrieval.chunk.content = EXCLUDED.content
        THEN crv_retrieval.chunk.embedding_m3 END
WHERE crv_retrieval.chunk.payload_sha256 IS DISTINCT FROM EXCLUDED.payload_sha256
"""
_PENDING = """
SELECT source.evidence_id, source.content_hash,
    jsonb_object_agg(source.projection_id, source.payload_sha256) AS expected
FROM ingest.projection_item AS source
LEFT JOIN ingest.projection_item AS copy
    ON copy.store = $4 AND copy.target = $5 AND copy.projection_id = source.projection_id
    AND copy.state = 'verified'
WHERE source.store = $1 AND source.target = $2 AND source.release_id = $3
    AND source.state = 'verified'
GROUP BY source.evidence_id, source.content_hash
HAVING $6 OR bool_or(copy.projection_id IS NULL)
ORDER BY source.content_hash, source.evidence_id
LIMIT $7
"""
VECTOR_INDEXES: Mapping[str, tuple[str, str, str]] = {
    "te3": ("chunk_te3_hnsw_idx", "(embedding_te3::halfvec(3072)) halfvec_cosine_ops",
            "embedding_te3"),
    "m3": ("chunk_m3_hnsw_idx", "embedding_m3 vector_cosine_ops", "embedding_m3"),
}
_MEMORY = re.compile(r"^[1-9][0-9]{0,4}(MB|GB)$")


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    record = {name: row.get(name) for name, _ in _COLUMNS}
    for name in _LISTS:
        record[name] = list(record[name] or [])
    record["is_generated"] = bool(record["is_generated"])
    return record


def _problem(row: Mapping[str, Any]) -> str | None:
    missing = [name for name in _REQUIRED if not row.get(name)]
    if missing:
        return f"missing {', '.join(missing)}"
    vector = row.get("content_vector")
    if vector is None or len(vector) != TE3_DIMENSIONS:
        return f"content_vector must have {TE3_DIMENSIONS} dimensions"
    return None


def _check_fields(key_field: str, digest_field: str | None = None) -> None:
    if key_field != KEY_FIELD or digest_field not in (None, DIGEST_FIELD):
        raise InvalidInputError("crv_retrieval rows are keyed by projection_id/payload_sha256")


class PostgresRetrievalProjector:
    """``SearchProjector`` over ``crv_retrieval.chunk``: one statement per batch of rows."""

    def __init__(self, pool: PgPool, clock: Clock) -> None:
        self._pool = pool
        self._clock = clock

    async def upload(
        self, documents: Sequence[Mapping[str, Any]], *, key_field: str
    ) -> ProjectionReport:
        _check_fields(key_field)
        permanent: dict[str, str] = {}
        records: list[dict[str, Any]] = []
        for document in documents:
            problem = _problem(document)
            if problem is not None:
                permanent[str(document.get(KEY_FIELD) or "")] = problem
            else:
                records.append(_record(document))
        keys = [record[KEY_FIELD] for record in records]
        if len(set(keys)) != len(keys):
            raise InvalidInputError("Duplicate document keys in one projection request")
        if records:
            async with self._pool.acquire() as connection:
                await connection.execute(_UPSERT, records, self._clock.now())
        return ProjectionReport(accepted=tuple(keys), permanent=permanent)

    async def delete(self, keys: Sequence[str], *, key_field: str) -> ProjectionReport:
        _check_fields(key_field)
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM crv_retrieval.chunk WHERE projection_id = ANY($1::text[])",
                list(keys),
            )
        return ProjectionReport(accepted=tuple(keys))

    async def verify(
        self, expected: Mapping[str, str], *, key_field: str, digest_field: str
    ) -> dict[str, str]:
        _check_fields(key_field, digest_field)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT projection_id, payload_sha256, embedding_te3 IS NOT NULL AS has_vector"
                " FROM crv_retrieval.chunk WHERE projection_id = ANY($1::text[])",
                list(expected),
            )
        found = {row["projection_id"]: row for row in rows}
        problems: dict[str, str] = {}
        for key, digest in expected.items():
            row = found.get(key)
            if row is None:
                problems[key] = "missing"
            elif row["payload_sha256"] != digest:
                problems[key] = "digest_mismatch"
            elif not row["has_vector"]:
                problems[key] = "missing_vector"
        return problems


@dataclass(frozen=True)
class PendingContent:
    evidence_id: str
    content_hash: str
    expected: dict[str, str]
    """Search rows of the content: projection_id -> payload digest."""


async def pending_contents(
    pool: PgPool,
    *,
    source_store: str,
    source_target: str,
    release_id: str,
    limit: int,
    refresh: bool,
) -> list[PendingContent]:
    """Contents with verified Search rows of the release and no verified copy (all if refresh)."""

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            _PENDING, source_store, source_target, release_id, STORE, TARGET, refresh, limit
        )
    return [
        PendingContent(row["evidence_id"], row["content_hash"], dict(row["expected"]))
        for row in rows
    ]


async def build_vector_index(
    connection: PgConnection,
    model: str,
    *,
    m: int,
    ef_construction: int,
    maintenance_work_mem: str,
    parallel_workers: int,
) -> dict[str, Any]:
    """Create the model's HNSW index (after the bulk load) and refresh planner statistics."""

    if model not in VECTOR_INDEXES:
        raise InvalidInputError(f"Unknown embedding model {model!r}")
    if not (2 <= m <= 100 and 4 <= ef_construction <= 1000 and 0 <= parallel_workers <= 32):
        raise InvalidInputError("HNSW parameters out of range")
    if not _MEMORY.match(maintenance_work_mem):
        raise InvalidInputError("maintenance_work_mem must look like 512MB or 4GB")
    name, expression, column = VECTOR_INDEXES[model]
    await connection.execute("SET statement_timeout = 0")
    await connection.execute(f"SET maintenance_work_mem = '{maintenance_work_mem}'")
    await connection.execute(f"SET max_parallel_maintenance_workers = {parallel_workers}")
    await connection.execute(
        f"CREATE INDEX IF NOT EXISTS {name} ON crv_retrieval.chunk USING hnsw ({expression})"
        f" WITH (m = {m}, ef_construction = {ef_construction})"
    )
    await connection.execute("ANALYZE crv_retrieval.chunk")
    counts = await connection.fetchrow(
        f"SELECT count(*) AS rows, count({column}) AS vectors FROM crv_retrieval.chunk"
    )
    size = await connection.fetchval(
        "SELECT pg_size_pretty(pg_relation_size(to_regclass($1)))", f"crv_retrieval.{name}"
    )
    return {
        "index": name, "model": model, "m": m, "ef_construction": ef_construction,
        "rows": counts["rows"], "vectors": counts["vectors"], "size": size,
    }
