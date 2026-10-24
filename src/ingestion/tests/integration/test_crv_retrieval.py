"""crv_retrieval (migration 0022) against a real PostgreSQL 16 + pgvector: the Search-parity text
analysis, fusion, scopes, table neighbours, index build and the reader's execute-only access."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ingest_pipeline.adapters.memory.clock import ManualClock
from ingest_pipeline.adapters.postgres.retrieval import (
    PostgresRetrievalProjector,
    build_vector_index,
)

pytestmark = pytest.mark.integration

RELEASE = "crv-v1"
A, B, C = "a" * 64, "b" * 64, "c" * 64
SEARCH = "SELECT * FROM crv_retrieval.search($1, $2, $3::text[], $4, $5::real[], $6)"


def _vector(axis: int) -> list[float]:
    vector = [0.0] * 3072
    vector[axis] = 1.0
    return vector


def _row(key: str, content_hash: str, content: str, axis: int, **changes: Any) -> dict[str, Any]:
    return {
        "projection_id": key, "release_id": RELEASE, "chunk_id": f"chunk:{key}",
        "content_hash": content_hash, "chunk_kind": "prose", "title": f"{key}.pdf - p. 1",
        "content": content, "source_uri": f"raw/{key}.pdf", "source_path": f"Dossier/{key}.pdf",
        "source_paths": [f"Dossier/{key}.pdf"], "document_code": None, "identifiers": [],
        "original_page_numbers": [1], "source_locators": [], "original_source_uris": [],
        "original_content_hashes": [content_hash], "is_generated": False,
        "document_class": "report", "citation_basis": "source", "extraction_route": "di_layout",
        "language": None, "payload_sha256": "d" * 64, "content_vector": _vector(axis),
        **changes,
    }


ROWS = [
    _row("flotation", A, "Essais de flottation du cuivre à Bleida : récupération de 92 %.", 0,
         language="fr", identifiers=["RA-2019-05"], document_code="RA-2019-05"),
    _row("leaching", B, "Leaching tests of cobalt concentrate at 80 °C.", 1, language="en"),
    _row("table", C, "| Essai | Cu % |\n| 1 | 92 |", 2, chunk_kind="table",
         original_page_numbers=[3]),
    _row("generated", C, "Tableau généré", 3, chunk_kind="table", original_page_numbers=[3],
         is_generated=True),
    _row("other-release", A, "Essais de flottation", 4, release_id="crv-v0"),
]


@pytest.fixture
async def loaded(pg_pool: Any) -> Any:
    from datetime import UTC, datetime

    projector = PostgresRetrievalProjector(pg_pool, ManualClock(datetime(2026, 10, 23, tzinfo=UTC)))
    report = await projector.upload(ROWS, key_field="projection_id")
    assert len(report.accepted) == len(ROWS)
    return pg_pool


async def _search(
    pool: Any, text: str, *, hashes: list[str] | None = None, axis: int | None = None,
    top: int = 10,
) -> list[str]:
    vector = None if axis is None else _vector(axis)
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            SEARCH, RELEASE, text, hashes or [], "te3" if vector else None, vector, top
        )
    return [row["projection_id"] for row in rows]


async def test_keyword_search_stems_folds_accents_and_ignores_stop_words(loaded: Any) -> None:
    assert (await _search(loaded, "les flottations à BLEIDA"))[0] == "flotation"
    assert (await _search(loaded, "recuperation"))[0] == "flotation"
    assert await _search(loaded, "leaching of the cobalt") == ["leaching"]
    assert await _search(loaded, "le la de the of") == []


async def test_identifier_leg_matches_whole_codes_like_the_search_analyzer(loaded: Any) -> None:
    async with loaded.acquire() as connection:
        keys = await connection.fetchval(
            "SELECT crv_retrieval.query_identifiers($1)", "Voir le rapport ra-2019-05."
        )
    assert "ra-2019-05" in keys
    assert await _search(loaded, "ra-2019-05") == ["flotation"]


async def test_vector_leg_and_scopes(loaded: Any) -> None:
    assert (await _search(loaded, "zzz", axis=1))[0] == "leaching"
    assert set(await _search(loaded, "flottation", hashes=[C], axis=2)) <= {"table", "generated"}
    async with loaded.acquire() as connection:
        with pytest.raises(Exception, match="Invalid content hash filter"):
            await connection.fetch(SEARCH, RELEASE, "x", ["' OR true --"], None, None, 10)


async def test_table_neighbors_and_release_status(loaded: Any) -> None:
    async with loaded.acquire() as connection:
        rows = await connection.fetch(
            "SELECT projection_id FROM crv_retrieval.table_neighbors($1, $2::jsonb)",
            RELEASE, json.dumps({C: [2, 3, 4], A: [1]}),
        )
        status = await connection.fetchrow(
            "SELECT * FROM crv_retrieval.release_status($1)", RELEASE
        )
    assert [row["projection_id"] for row in rows] == ["table"]
    assert dict(status) == {"chunks": 4, "te3": 4, "m3": 0}


async def test_hnsw_index_serves_the_same_nearest_rows(loaded: Any) -> None:
    async with loaded.acquire() as connection:
        report = await build_vector_index(
            connection, "te3", m=16, ef_construction=64, maintenance_work_mem="64MB",
            parallel_workers=0,
        )
        await connection.execute("RESET maintenance_work_mem")
        await connection.execute("RESET statement_timeout")
    assert (report["rows"], report["vectors"]) == (5, 5)
    assert (await _search(loaded, "zzz", axis=1))[0] == "leaching"


async def test_reader_executes_search_but_cannot_read_the_table(loaded: Any) -> None:
    import asyncpg

    async with loaded.acquire() as connection:
        await connection.execute("SET ROLE crv_kg_reader")
        try:
            rows = await connection.fetch(SEARCH, RELEASE, "flottation", [], None, None, 5)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT * FROM crv_retrieval.chunk")
        finally:
            await connection.execute("RESET ROLE")
    assert rows[0]["projection_id"] == "flotation"
