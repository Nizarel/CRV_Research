"""Shared fixtures for pipeline tests: an in-memory harness and an optional AGE database."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from pipeline_harness import Harness, HarnessFactory, build_harness

PG_DOCKERFILE = Path(__file__).resolve().parent / "integration" / "postgres.Dockerfile"
"""PostgreSQL 16 + Apache AGE 1.6.0 (pinned there) + pgvector for migration 0022."""


@pytest.fixture
def make_harness() -> HarnessFactory:
    return build_harness


@pytest.fixture
def harness() -> Harness:
    return build_harness()


# --- PostgreSQL with Apache AGE (integration marker only) ---------------------------------

LEDGER_TABLES = (
    "crv_retrieval.chunk",
    "crv_retrieval.chunk_terms",
    "crv_retrieval.lexeme_stats",
    "crv_retrieval.release_stats",
    "ingest.extraction_cache",
    "ingest.job_replay",
    "ingest.legacy_reconciliation",
    "ingest.projection_item",
    "ingest.figure_asset",
    "ingest.embedding",
    "ingest.chunk",
    "ingest.evidence_document",
    "ingest.plan_barrier",
    "ingest.unit",
    "ingest.plan",
    "ingest.outbox",
    "ingest.job_artifact",
    "ingest.artifact",
    "ingest.stage_attempt",
    "ingest.stage_job",
    "ingest.source_snapshot",
)


def _baseline_sql() -> str:
    configured = os.environ.get("TIZ_TEST_BASELINE_SQL")
    if configured:
        return Path(configured).read_text(encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / "infra/sql/ingestion-catalog/0002_knowledge_prerequisites.sql").read_text(
        encoding="utf-8"
    )


async def _bootstrap(dsn: str) -> None:
    import asyncpg

    from ingest_pipeline.adapters.postgres.migrations import (
        BASELINE_VERSION,
        Migration,
        apply_migrations,
        load_migrations,
        normalize_sql,
    )

    connection = await asyncpg.connect(dsn)
    try:
        if await connection.fetchval("SELECT to_regclass('ingest.schema_migration')") is None:
            baseline = Migration(BASELINE_VERSION, normalize_sql(_baseline_sql()))
            async with connection.transaction():
                await connection.execute(baseline.sql)
                await connection.execute(
                    "INSERT INTO ingest.schema_migration (version, sha256) VALUES ($1, $2)",
                    baseline.version,
                    baseline.sha256,
                )
        await apply_migrations(connection, load_migrations(), dry_run=False)
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    pytest.importorskip("asyncpg", reason="asyncpg is unavailable (no wheel on this platform)")
    configured = os.environ.get("TIZ_TEST_PG_DSN")
    if configured:
        asyncio.run(_bootstrap(configured))
        yield configured
        return
    postgres = pytest.importorskip("testcontainers.postgres")
    images = pytest.importorskip("testcontainers.core.image")
    try:
        image = images.DockerImage(
            path=str(PG_DOCKERFILE.parent), dockerfile_path=PG_DOCKERFILE.name,
            tag="crv-pipeline-postgres:test",
        )
        image.build()
    except Exception as error:  # Docker unavailable
        pytest.skip(f"Cannot build the PostgreSQL test image: {error}")
    container = postgres.PostgresContainer(
        str(image), username="postgres", password="postgres", dbname="ingestion-catalog",
        driver=None,
    ).with_command("postgres -c jit=off")  # as on Azure (see integration/compose.yaml)
    try:
        container.start()
    except Exception as error:  # Docker unavailable
        image.remove()
        pytest.skip(f"Cannot start AGE container: {error}")
    try:
        dsn = container.get_connection_url()
        asyncio.run(_bootstrap(dsn))
        yield dsn
    finally:
        container.stop()
        image.remove()


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[Any]:
    from pydantic import SecretStr

    from ingest_pipeline.adapters.postgres.pool import create_pool
    from ingest_pipeline.config import DatabaseSettings

    pool = await create_pool(DatabaseSettings(dsn=SecretStr(pg_dsn), max_pool_size=8))
    async with pool.acquire() as connection:
        await connection.execute(f"TRUNCATE {', '.join(LEDGER_TABLES)}")
    try:
        yield pool
    finally:
        await pool.close()
