"""Schema and migration behaviour against a real PostgreSQL 16 + Apache AGE 1.6.0."""

from __future__ import annotations

from typing import Any

import pytest

from tizertmaint_pipeline.adapters.postgres.migrations import (
    BASELINE_VERSION,
    Migration,
    apply_migrations,
    load_migrations,
)
from tizertmaint_pipeline.domain.errors import IntegrityError

pytestmark = pytest.mark.integration


async def test_baseline_and_packaged_migrations_are_registered(pg_pool: Any) -> None:
    async with pg_pool.acquire() as connection:
        versions = {
            row["version"]
            for row in await connection.fetch("SELECT version FROM ingest.schema_migration")
        }
        age_version = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'age'"
        )
    assert {BASELINE_VERSION, *(m.version for m in load_migrations())} <= versions
    assert age_version == "1.6.0"


async def test_reapplying_is_a_no_op(pg_pool: Any) -> None:
    migrations = load_migrations()
    async with pg_pool.acquire() as connection:
        report = await apply_migrations(connection, migrations, dry_run=False)
    assert report.applied == []
    assert report.already_applied == [m.version for m in migrations]


async def test_dry_run_rolls_back_new_migrations(pg_pool: Any) -> None:
    probe = Migration("0999_dry_run_probe", "CREATE TABLE ingest.dry_run_probe (id INT);")
    async with pg_pool.acquire() as connection:
        report = await apply_migrations(connection, [*load_migrations(), probe], dry_run=True)
        exists = await connection.fetchval("SELECT to_regclass('ingest.dry_run_probe')")
        registered = await connection.fetchval(
            "SELECT count(*) FROM ingest.schema_migration WHERE version = $1", probe.version
        )
    assert report.applied == [probe.version]
    assert (exists, registered) == (None, 0)


async def test_changed_migration_is_refused(pg_pool: Any) -> None:
    [first, *rest] = load_migrations()
    drifted = Migration(first.version, first.sql + "\n-- edited after release\n")
    async with pg_pool.acquire() as connection:
        with pytest.raises(IntegrityError):
            await apply_migrations(connection, [drifted, *rest], dry_run=False)


async def test_lease_columns_must_match_the_status(pg_pool: Any) -> None:
    import asyncpg

    async with pg_pool.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO ingest.stage_job (job_id, unit_id, stage, fingerprint, pool, status,
                    not_before, max_failures, created_at, updated_at)
                VALUES ('j', 'u', 'snapshot', repeat('a', 64), 'io', 'leased',
                    now(), 3, now(), now())
                """
            )


async def test_runtime_role_cannot_delete_history(pg_pool: Any) -> None:
    async with pg_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT relation, privilege,
                   has_table_privilege('tizert_ingest_worker', relation, privilege) AS granted
            FROM unnest(ARRAY['ingest.stage_job', 'ingest.outbox', 'ingest.source_snapshot'])
                     AS relation,
                 unnest(ARRAY['INSERT', 'DELETE', 'TRUNCATE']) AS privilege
            """
        )
    granted = {(row["relation"], row["privilege"]): row["granted"] for row in rows}
    assert all(granted[(relation, "INSERT")] for relation in ("ingest.stage_job", "ingest.outbox"))
    assert not any(value for (_, privilege), value in granted.items() if privilege != "INSERT")
