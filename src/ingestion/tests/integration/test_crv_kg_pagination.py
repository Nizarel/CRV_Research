from datetime import UTC, date, datetime
from hashlib import sha256
from importlib.resources import files
from typing import Any

import pytest

from ingest_pipeline.adapters.postgres.crv_kg import CrvKgStore
from ingest_pipeline.domain.crv_kg.build import DocumentInput, Placement, build_run
from ingest_pipeline.domain.crv_kg.gazetteer import parse_gazetteer
from ingest_pipeline.domain.crv_kg.vocab import DEFAULT_RELEASE_STATUSES

pytestmark = pytest.mark.integration


async def test_document_pagination_is_scoped_stable_and_execute_only(pg_pool: Any) -> None:
    gazetteer = parse_gazetteer(
        files("ingest_pipeline").joinpath("data/crv-gazetteer.json").read_bytes(),
    )
    documents = [
        DocumentInput(
            sha256(f"pagination:{i}".encode()).hexdigest(), f"pagination:{i}", 2,
            (Placement(
                "AI for Science_ Geology & Chemistry/Geometallurgy Cu/"
                f"Dossier Cu/Bouskour/Rapport pagination {i}- 01-2020-.pdf"
            ),), (), (),
        ) for i in range(405)
    ]
    run, _ = build_run(documents, gazetteer, today=date(2026, 10, 21))
    store = CrvKgStore(pg_pool)
    now = datetime.now(UTC)
    await store.import_run(run, started_at=now, now=now)
    release = await store.build_release(
        statuses=DEFAULT_RELEASE_STATUSES, started_at=now, now=now,
    )
    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE crv_kg_reader")
        scoped = await connection.fetch(
            "SELECT * FROM crv_kg.list_documents($1, $2, NULL, NULL, 201)",
            release.release_id, ["scope:unavailable"],
        )
        assert not scoped
        after = None
        seen: list[str] = []
        while True:
            page = await connection.fetch(
                "SELECT * FROM crv_kg.list_documents($1, $2, NULL, $3, 200)",
                release.release_id, ["crv:geometallurgy"], after,
            )
            if not page:
                break
            assert len(page) <= 200
            seen.extend(row["iri"] for row in page)
            after = page[-1]["iri"]
        assert len(seen) >= 405
        assert seen == sorted(set(seen))
        assert not await connection.fetchval(
            "SELECT has_table_privilege(current_user, 'crv_kg.vertex_projection', 'SELECT')",
        )
