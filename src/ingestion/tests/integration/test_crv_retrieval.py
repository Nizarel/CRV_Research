"""crv_retrieval (migration 0022) against a real PostgreSQL 16 + pgvector: the Search-parity text
analysis, fusion, scopes, table neighbours, index build and the reader's execute-only access."""

from __future__ import annotations

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
            RELEASE, {C: [2, 3, 4], A: [1]},
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


async def test_keyword_terms_follow_every_write_of_the_worker(pg_pool: Any) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    terms = ("SELECT t.projection_id, t.release_id, t.content_hash, t.search_text = c.search_text"
             " AS same FROM crv_retrieval.chunk_terms AS t"
             " LEFT JOIN crv_retrieval.chunk AS c USING (projection_id) ORDER BY 1")
    changed = _row("leaching", C, "Essais de lixiviation du cobalt.", 1, language="fr",
                   payload_sha256="e" * 64)
    async with pg_pool.acquire() as connection:

        class Worker:
            @asynccontextmanager
            async def acquire(self) -> Any:
                yield connection

        projector = PostgresRetrievalProjector(
            Worker(), ManualClock(datetime(2026, 10, 27, tzinfo=UTC)))
        await connection.execute("SET ROLE tizert_ingest_worker")
        try:
            await projector.upload(ROWS[:2], key_field="projection_id")
            first = [tuple(row) for row in await connection.fetch(terms)]
            await projector.upload([changed], key_field="projection_id")
            await projector.delete(["flotation"], key_field="projection_id")
            last = [tuple(row) for row in await connection.fetch(terms)]
        finally:
            await connection.execute("RESET ROLE")
    assert first == [("flotation", RELEASE, A, True), ("leaching", RELEASE, B, True)]
    assert last == [("leaching", RELEASE, C, True)]
    assert await _search(pg_pool, "lixiviation") == ["leaching"]
    assert await _search(pg_pool, "flottation") == []


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


PROJECTION_ITEMS = """
INSERT INTO ingest.projection_item (store, target, projection_id, release_id, evidence_id,
    chunk_id, placement_id, payload_sha256, state, created_at, updated_at, content_hash)
SELECT $1, $2, c.projection_id, c.release_id, 'evd:' || c.projection_id, c.chunk_id, 'plc:1',
    c.payload_sha256, 'verified', now(), now(), c.content_hash
FROM crv_retrieval.chunk AS c WHERE c.release_id = $3
"""


async def test_verify_mirrors_the_vector_leg_and_gates_parity(loaded: Any) -> None:
    from ingest_pipeline.adapters.postgres.retrieval_checks import (
        _APPROXIMATE,
        CANDIDATES,
        CheckOptions,
        verify_release,
    )

    options = CheckOptions(release_id=RELEASE, source_store="azure_search",
                           source_target="crv-evidence-v1", sample=4, k=3)
    async with loaded.acquire() as connection:
        await connection.execute(PROJECTION_ITEMS, "azure_search", "crv-evidence-v1", RELEASE)
        await connection.execute(PROJECTION_ITEMS, "postgres", "crv_retrieval", RELEASE)
        await build_vector_index(
            connection, "te3", m=16, ef_construction=64, maintenance_work_mem="64MB",
            parallel_workers=0,
        )
        await connection.execute("RESET maintenance_work_mem")
        await connection.execute("RESET statement_timeout")
        mirrored = await connection.fetch(_APPROXIMATE, RELEASE, _vector(1), CANDIDATES, 4)
        # The checks plan under the function's own settings (0023), even on four rows.
        report = await verify_release(
            connection, ["preflight", "parity", "recall", "explain"], options
        )
        await connection.execute(
            "UPDATE crv_retrieval.chunk SET payload_sha256 = $1 WHERE projection_id = $2",
            "e" * 64, "leaching",
        )
        drifted = await verify_release(connection, ["parity"], options)

    assert [row["projection_id"] for row in mirrored] == await _search(loaded, "", axis=1, top=4)
    failed = {name: part for name, part in report.items() if isinstance(part, dict)
              and not part["ok"]}
    assert report["ok"] is True, failed
    assert report["preflight"]["has_0022"] is True
    assert report["parity"]["source_rows"] == 4
    assert report["recall"]["sampled"]["recall"] == 1.0
    assert "chunk_te3_hnsw_idx" in report["explain"]["unscoped"]["indexes"]
    assert report["explain"]["settings"]["enable_seqscan"] == "off"
    assert report["explain"]["settings"]["hnsw.ef_search"] == "500"
    assert (drifted["ok"], drifted["parity"]["digest_mismatches"]) == (False, 1)


async def test_keyword_leg_skips_words_found_in_most_chunks(pg_pool: Any) -> None:
    from datetime import UTC, datetime

    rows = [
        _row(f"row-{n}", f"{n % 3 + 1:064x}",
             "Rapport sur le zirconium." if n == 7 else "Rapport sur la flottation.", n)
        for n in range(300)
    ]
    projector = PostgresRetrievalProjector(pg_pool, ManualClock(datetime(2026, 10, 26, tzinfo=UTC)))
    assert len((await projector.upload(rows, key_field="projection_id")).accepted) == 300

    async def terms(text: str) -> str | None:
        async with pg_pool.acquire() as connection:
            return await connection.fetchval("SELECT crv_retrieval.match_terms($1)::text", text)

    async def search(text: str, hashes: list[str] | None = None) -> list[str]:
        async with pg_pool.acquire() as connection:
            found = await connection.fetch(SEARCH, RELEASE, text, hashes or [], None, None, 10)
        return [row["projection_id"] for row in found]

    # Without statistics every word matches.
    assert "rapport" in (await terms("rapport zirconium") or "")
    async with pg_pool.acquire() as connection:
        await connection.execute("ANALYZE crv_retrieval.chunk")

    assert await terms("rapport zirconium") == "'zirconium'"
    # Only common words: the least common one stays.
    assert (await terms("rapport flottation") or "").count("'") == 2
    assert await terms("le la de") is None
    assert (await search("rapport zirconium"))[0] == "row-7"
    # A scope is small, so it keeps every word.
    scoped = await search("rapport", [f"{1:064x}"])
    assert scoped
    assert set(scoped) <= {f"row-{n}" for n in range(300) if n % 3 == 0}
    # The words of query_terms are the lexemes of query_lexemes.
    async with pg_pool.acquire() as connection:
        same = await connection.fetchval(
            "SELECT crv_retrieval.query_terms(t) = "
            "(SELECT replace(array_to_tsvector(crv_retrieval.query_lexemes(t))::text, "
            "''' ''', ''' | ''')::tsquery) FROM (SELECT 'Les essais de flottation à 80 °C') AS q(t)"
        )
    assert same is True


async def test_keyword_leg_ranks_with_bm25_once_statistics_exist(pg_pool: Any) -> None:
    from datetime import UTC, datetime

    rows = [_row(f"row-{n}", f"{n % 3 + 1:064x}", "Rapport sur la flottation.", n)
            for n in range(200)]
    rows += [
        # The word in the title only, which every chunk of a document repeats.
        _row("titled", "e" * 64, "Analyse granulometrique des rejets.", 300,
             title="Bleida minerai.pdf - p. 1"),
        # The word twice in the text.
        _row("texted", "f" * 64, "Le minerai de Bleida et les essais de Bleida.", 301),
    ]
    projector = PostgresRetrievalProjector(pg_pool, ManualClock(datetime(2026, 10, 26, tzinfo=UTC)))
    assert len((await projector.upload(rows, key_field="projection_id")).accepted) == 202

    async def keyword(text: str) -> list[str]:
        async with pg_pool.acquire() as connection:
            found = await connection.fetch(SEARCH, RELEASE, text, [], None, None, 2)
        return [row["projection_id"] for row in found]

    # Without statistics the leg keeps ts_rank, which weighs the title five times the text.
    assert await keyword("Bleida") == ["titled", "texted"]
    async with pg_pool.acquire() as connection:
        refreshed = await connection.fetchval("SELECT crv_retrieval.refresh_keyword_stats()")
        frequency = await connection.fetchval(
            "SELECT chunks FROM crv_retrieval.lexeme_stats"
            " WHERE release_id = $1 AND lexeme = 'bleida'", RELEASE)
    assert refreshed[RELEASE]["chunks"] == 202
    assert frequency == 2
    assert await keyword("Bleida") == ["texted", "titled"]


async def test_m3_backfill_index_and_search(loaded: Any) -> None:
    from datetime import UTC, datetime

    from ingest_pipeline.adapters.memory.evidence import InMemoryEvidenceStore
    from ingest_pipeline.adapters.postgres.retrieval import RetrievalVectorStore
    from ingest_pipeline.runtime.retrieval_embed import EmbedOptions, embed_release

    axes = {row["content"]: index for index, row in enumerate(ROWS)}

    class Provider:
        model, dimensions = "BAAI/bge-m3", 1024

        async def embed(self, texts: Any) -> list[tuple[float, ...]]:
            vectors = []
            for text in texts:
                vector = [0.0] * 1024
                vector[axes[text]] = 1.0
                vectors.append(tuple(vector))
            return vectors

    store = RetrievalVectorStore(loaded)
    clock = ManualClock(datetime(2026, 10, 28, tzinfo=UTC))
    report = await embed_release(store, Provider(), InMemoryEvidenceStore(), clock,
                                 EmbedOptions(RELEASE, batch_rows=2))
    assert (report.selected, report.written, report.failed) == (4, 4, 0)
    assert report.after == {"chunks": 4, "te3": 4, "m3": 4}
    async with loaded.acquire() as connection:
        await connection.execute(
            "UPDATE crv_retrieval.chunk SET content = 'changed', embedding_m3 = NULL"
            " WHERE projection_id = 'table'"
        )
        # A vector embedded from the old text must not land on the changed row.
        stale = await store.write([{"projection_id": "table",
                                    "content_sha256": "0" * 64, "vector": [0.5] * 1024}])
        assert stale == 0
        await connection.execute(
            "UPDATE crv_retrieval.chunk SET content = $1 WHERE projection_id = 'table'",
            ROWS[2]["content"],
        )
        index = await build_vector_index(
            connection, "m3", m=16, ef_construction=64, maintenance_work_mem="64MB",
            parallel_workers=0,
        )
        await connection.execute("RESET maintenance_work_mem")
        await connection.execute("RESET statement_timeout")
    rerun = await embed_release(store, Provider(), InMemoryEvidenceStore(), clock,
                                EmbedOptions(RELEASE))
    assert (rerun.selected, rerun.written) == (1, 1)
    assert index["index"] == "chunk_m3_hnsw_idx"
    query = [0.0] * 1024
    query[1] = 1.0
    async with loaded.acquire() as connection:
        rows = await connection.fetch(SEARCH, RELEASE, "zzz", [], "m3", query, 3)
    assert rows[0]["projection_id"] == "leaching"

    from ingest_pipeline.adapters.postgres.retrieval_checks import CheckOptions, verify_release

    options = CheckOptions(release_id=RELEASE, source_store="azure_search",
                           source_target="crv-evidence-v1", sample=4, k=3, model="m3")
    async with loaded.acquire() as connection:
        report = await verify_release(connection, ["recall", "explain"], options)
    assert report["recall"]["sampled"]["queries"] == 4
    assert report["recall"]["ok"] is True