"""Read-only inventory of the v1 ingestion catalog (public schema) and its reconciliation rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ...domain.legacy import LegacyItem
from .pool import PgPool

_INVENTORY = """
SELECT source.content_hash,
       COALESCE(current.ingestion_state, source.ingestion_state) AS legacy_state,
       (SELECT count(*) FROM public.chunk AS c WHERE c.content_hash = source.content_hash)
           AS chunks,
       (SELECT count(*) FROM public.page AS p WHERE p.content_hash = source.content_hash)
           AS pages,
       (SELECT count(*) FROM public.figure AS f WHERE f.content_hash = source.content_hash)
           AS figures,
       (SELECT evidence.evidence_id FROM ingest.evidence_document AS evidence
        WHERE evidence.content_hash = source.content_hash AND evidence.status = 'embedded'
        ORDER BY evidence.updated_at DESC LIMIT 1) AS v2_evidence_id
FROM public.source_object AS source
LEFT JOIN public.state AS current ON current.content_hash = source.content_hash
WHERE source.content_hash ~ '^[0-9a-f]{64}$'
ORDER BY source.content_hash
LIMIT $1
"""

_UPSERT = """
INSERT INTO ingest.legacy_reconciliation (legacy_key, content_hash, legacy_state, state,
    evidence, reason, checked_at)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
ON CONFLICT (legacy_key) DO UPDATE
SET legacy_state = EXCLUDED.legacy_state, state = EXCLUDED.state, evidence = EXCLUDED.evidence,
    reason = EXCLUDED.reason, checked_at = EXCLUDED.checked_at
"""

_LEGACY_TABLES = ("source_object", "state", "chunk", "page", "figure")


class LegacyCatalog:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def present(self) -> bool:
        async with self._pool.acquire() as connection:
            for table in _LEGACY_TABLES:
                if await connection.fetchval("SELECT to_regclass($1)", f"public.{table}") is None:
                    return False
        return True

    async def inventory(self, *, limit: int) -> list[LegacyItem]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(_INVENTORY, limit)
        return [
            LegacyItem(
                content_hash=row["content_hash"],
                legacy_state=row["legacy_state"],
                chunks=int(row["chunks"]),
                pages=int(row["pages"]),
                figures=int(row["figures"]),
                v2_evidence_id=row["v2_evidence_id"],
            )
            for row in rows
        ]

    async def record(self, items: Sequence[LegacyItem], *, now: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            for item in items:
                await connection.execute(
                    _UPSERT,
                    item.legacy_key,
                    item.content_hash,
                    item.legacy_state,
                    item.state.value,
                    {
                        "chunks": item.chunks,
                        "pages": item.pages,
                        "figures": item.figures,
                        "v2_evidence_id": item.v2_evidence_id,
                    },
                    item.reason,
                    now,
                )
