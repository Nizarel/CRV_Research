"""v1 catalog inventory against a real PostgreSQL (legacy tables are created and dropped here)."""

from __future__ import annotations

from typing import Any

import pytest
from pipeline_harness import START

from ingest_pipeline.adapters.postgres.legacy import LegacyCatalog
from ingest_pipeline.domain.legacy import LegacyState

pytestmark = pytest.mark.integration

LEGACY_DDL = """
CREATE TABLE public.source_object (content_hash TEXT PRIMARY KEY, ingestion_state TEXT NOT NULL);
CREATE TABLE public.state (content_hash TEXT PRIMARY KEY, ingestion_state TEXT NOT NULL);
CREATE TABLE public.chunk (chunk_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
CREATE TABLE public.page (id TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
CREATE TABLE public.figure (id TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
"""
DROP = (
    "DROP TABLE IF EXISTS public.source_object, public.state, public.chunk, public.page,"
    " public.figure"
)


async def test_inventory_classifies_without_touching_legacy_rows(pg_pool: Any) -> None:
    unknown, available, verified = "1" * 64, "2" * 64, "3" * 64
    catalog = LegacyCatalog(pg_pool)
    async with pg_pool.acquire() as connection:
        await connection.execute(DROP)
        assert not await catalog.present()
        await connection.execute(LEGACY_DDL)
        for content_hash in (unknown, available, verified):
            await connection.execute(
                "INSERT INTO public.source_object VALUES ($1, 'discovered')", content_hash
            )
        await connection.execute("INSERT INTO public.state VALUES ($1, 'quarantined')", unknown)
        await connection.execute("INSERT INTO public.chunk VALUES ('c1', $1)", available)
        await connection.execute("INSERT INTO public.chunk VALUES ('c2', $1)", verified)
        await connection.execute(
            "INSERT INTO ingest.plan (plan_id, content_hash, policy_version, family, unit_count,"
            " created_at) VALUES ('plan:v', $1, 'routing-v1', 'pdf', 0, $2)",
            verified,
            START,
        )
        await connection.execute(
            "INSERT INTO ingest.evidence_document (evidence_id, plan_id, content_hash,"
            " policy_version, completeness, status, manifest, chunk_set, chunk_count, created_at,"
            " updated_at) VALUES ('evd:v', 'plan:v', $1, 'routing-v1', 'complete', 'embedded',"
            " '{}'::jsonb, '{}'::jsonb, 1, $2, $2)",
            verified,
            START,
        )
    try:
        assert await catalog.present()
        items = {item.content_hash: item for item in await catalog.inventory(limit=10)}
        assert items[unknown].state is LegacyState.UNKNOWN
        assert items[unknown].legacy_state == "quarantined"
        assert items[available].state is LegacyState.EVIDENCE_AVAILABLE
        assert items[verified].state is LegacyState.VERIFIED
        await catalog.record(list(items.values()), now=START)
        await catalog.record(list(items.values()), now=START)
        async with pg_pool.acquire() as connection:
            recorded = await connection.fetch(
                "SELECT legacy_key, state FROM ingest.legacy_reconciliation ORDER BY legacy_key"
            )
            legacy_rows = await connection.fetchval("SELECT count(*) FROM public.source_object")
        assert [row["state"] for row in recorded] == [
            "legacy_unknown",
            "legacy_evidence_available",
            "verified",
        ]
        assert legacy_rows == 3
    finally:
        async with pg_pool.acquire() as connection:
            await connection.execute(DROP)
