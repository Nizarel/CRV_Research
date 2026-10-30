"""Seed a throwaway CRV ingestion-catalog for the DGX migration rehearsal (synthetic rows only).

Runs inside the crv-pipeline image against an empty ``ingestion-catalog`` database as a superuser
(TIZ_DB__DSN): the AGE baseline and every packaged migration, as the integration tests do;
SEED_ROWS crv_retrieval chunks with clustered 3,072-dimension vectors; verified projection items
for the Search and PostgreSQL stores (what retrieval-verify parity compares); the te3 HNSW index.
Prints one JSON report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from ingest_pipeline.adapters.memory.clock import ManualClock
from ingest_pipeline.adapters.postgres.migrations import (
    BASELINE_VERSION,
    Migration,
    apply_migrations,
    load_migrations,
    normalize_sql,
)
from ingest_pipeline.adapters.postgres.pool import create_pool
from ingest_pipeline.adapters.postgres.retrieval import (
    PostgresRetrievalProjector,
    build_vector_index,
)
from ingest_pipeline.config import DatabaseSettings
from pydantic import SecretStr

RELEASE = "crv-v1"
DIMENSIONS = 3072
DOCUMENTS = 97
BATCH = 100
TOPICS = (
    "Essais de flottation du minerai de cuivre : récupération et teneur du concentré.",
    "Leaching tests of cobalt concentrate at 80 °C with sulphuric acid.",
    "Campagne de sondages carottés, intervalles minéralisés et teneurs en argent.",
    "Bilan métallurgique du circuit de broyage et granulométrie du produit.",
    "Geochemical soil survey with copper and zinc anomalies along the fault.",
)
PROJECTION_ITEMS = """
INSERT INTO ingest.projection_item (store, target, projection_id, release_id, evidence_id,
    chunk_id, placement_id, payload_sha256, state, created_at, updated_at, content_hash)
SELECT $1, $2, c.projection_id, c.release_id, 'evd:' || c.projection_id, c.chunk_id, 'plc:1',
    c.payload_sha256, 'verified', now(), now(), c.content_hash
FROM crv_retrieval.chunk AS c WHERE c.release_id = $3
"""


def _normalized(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def _row(number: int, centroids: list[list[float]], rng: random.Random) -> dict[str, Any]:
    document = number % DOCUMENTS
    key = f"synthetic-{number:05d}"
    code = f"RA-2026-{document:03d}"
    content_hash = hashlib.sha256(f"document-{document}".encode()).hexdigest()
    page = number % 40 + 1
    vector = _normalized([c + rng.gauss(0.0, 0.35) for c in centroids[document]])
    return {
        "projection_id": key, "release_id": RELEASE, "chunk_id": f"chunk:{key}",
        "content_hash": content_hash, "chunk_kind": "table" if number % 11 == 0 else "prose",
        "title": f"{code}.pdf - p. {page}",
        "content": f"{TOPICS[number % len(TOPICS)]} Essai {number}, teneur "
                   f"{rng.uniform(0.1, 9.9):.2f} %, rapport {code}.",
        "source_uri": f"raw/synthetic/{code}.pdf", "source_path": f"Synthetic/{code}.pdf",
        "source_paths": [f"Synthetic/{code}.pdf"],
        "document_code": code if number % 5 == 0 else None,
        "identifiers": [code] if number % 5 == 0 else [],
        "original_page_numbers": [page], "source_locators": [], "original_source_uris": [],
        "original_content_hashes": [content_hash], "is_generated": number % 13 == 0,
        "document_class": "report", "citation_basis": "source", "extraction_route": "di_layout",
        "language": "fr" if number % 2 else "en",
        "payload_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "content_vector": vector,
    }


async def main(baseline_sql: str) -> None:
    dsn = os.environ["TIZ_DB__DSN"]
    rows = int(os.environ.get("SEED_ROWS", "2000"))
    baseline = Migration(BASELINE_VERSION, normalize_sql(baseline_sql))
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction():
            await connection.execute(baseline.sql)
            await connection.execute(
                "INSERT INTO ingest.schema_migration (version, sha256) VALUES ($1, $2)",
                baseline.version, baseline.sha256,
            )
        migrations = await apply_migrations(connection, load_migrations(), dry_run=False)
    finally:
        await connection.close()

    rng = random.Random(20261027)
    centroids = [_normalized([rng.gauss(0.0, 1.0) for _ in range(DIMENSIONS)])
                 for _ in range(DOCUMENTS)]
    pool = await create_pool(DatabaseSettings(dsn=SecretStr(dsn), max_pool_size=4))
    try:
        projector = PostgresRetrievalProjector(
            pool, ManualClock(datetime(2026, 10, 27, tzinfo=UTC))
        )
        accepted = 0
        for start in range(0, rows, BATCH):
            batch = [_row(n, centroids, rng) for n in range(start, min(start + BATCH, rows))]
            report = await projector.upload(batch, key_field="projection_id")
            if len(report.accepted) != len(batch):
                raise SystemExit(f"Rows rejected in batch {start}: {report}")
            accepted += len(batch)
        async with pool.acquire() as connection:
            await connection.execute(PROJECTION_ITEMS, "azure_search", "crv-evidence-v1", RELEASE)
            await connection.execute(PROJECTION_ITEMS, "postgres", "crv_retrieval", RELEASE)
            index = await build_vector_index(
                connection, "te3", m=16, ef_construction=64, maintenance_work_mem="512MB",
                parallel_workers=0,
            )
            await connection.execute("RESET maintenance_work_mem")
            await connection.execute("RESET statement_timeout")
            await connection.execute("ANALYZE")
    finally:
        await pool.close()
    print(json.dumps({
        "migrations_applied": len(migrations.applied) + 1,
        "last_migration": migrations.applied[-1] if migrations.applied else baseline.version,
        "rows": accepted,
        "index": {key: value for key, value in index.items()
                  if isinstance(value, (str, int, float, bool))},
    }))


if __name__ == "__main__":
    asyncio.run(main(Path(os.environ["BASELINE_SQL"]).read_text("utf-8")))
