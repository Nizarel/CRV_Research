from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, Rollback
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.postgres.memory import checkpointer
from crv_agent.adapters.postgres.persistence import PostgresPersistence
from crv_agent.domain.execution import Budget
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.graph import build_graph
from crv_agent.ports.persistence import ThreadNotFound, TurnConflict
from tests.fakes import FakeKnowledge, FakeModels, FakeWorker

pytestmark = pytest.mark.integration


async def test_postgres_ownership_idempotency_locking_and_checkpoints(pg_dsn: str) -> None:
    schema = "crv_test_" + uuid4().hex
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as connection:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    try:
        async with AsyncConnectionPool(
            pg_dsn, kwargs={
                "autocommit": True, "row_factory": dict_row,
                "options": f"-c search_path={schema}",
            }, open=False,
        ) as pool:
            async with pool.connection() as connection:
                await connection.execute(
                    files("crv_agent.adapters.postgres").joinpath("schema.sql").read_text(),
                )
            saver = checkpointer(pool)
            await saver.setup()
            store = PostgresPersistence(pool)
            thread = await store.create_thread("one", "s1", "kg1")
            with pytest.raises(ThreadNotFound):
                await store.history("two", thread.id)
            run, created = await store.start_run("one", thread.id, "r1", "Question")
            assert created and run.status == "running"
            assert not (await store.start_run("one", thread.id, "r1", "Question"))[1]
            with pytest.raises(TurnConflict):
                await store.start_run("one", thread.id, "r2", "Concurrent")
            with pytest.raises(TurnConflict):
                await store.start_run("one", thread.id, "r1", "Changed")
            await store.finish_run("one", thread.id, "r1", "completed", {"content": "Verified"})
            assert (await store.history("one", thread.id))[0].result == {"content": "Verified"}
            await store.feedback("one", thread.id, "r1", 1, "Useful")
            # Persistence survives independently constructed runtime/store objects.
            other = PostgresPersistence(pool)
            assert (await other.thread("one", thread.id)).search_release == "s1"
            assert (await other.start_run("one", thread.id, "r1", "Question"))[0].result
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": thread.id + ":checkpoint"}}
            await graph.ainvoke(
                {"question": "Recovery?", "history": []}, config,
                context=ResearchContext(
                    models=FakeModels(), knowledge=FakeKnowledge(), worker=FakeWorker(),
                    budget=Budget(), search_release="r1", kg_release="kg1", trace_id="test",
                ),
            )
            restarted = build_graph(checkpointer(pool))
            snapshot = await restarted.aget_state(config)
            assert snapshot.values["answer"].citations[0].pages == [4]
    finally:
        async with await AsyncConnection.connect(pg_dsn, autocommit=True) as connection:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_real_sql_pagination_scopes_and_reader_permissions(pg_dsn: str) -> None:
    migration = Path(__file__).resolve().parents[4].joinpath(
        "src", "ingestion", "ingest_pipeline", "sql", "0019_crv_kg_document_page.sql",
    ).read_text(encoding="utf-8")
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as connection:
        # Temporary transaction fixtures model only the tables used by this read function.
        async with connection.transaction():
            await connection.execute("""
                CREATE SCHEMA crv_kg;
                CREATE ROLE crv_kg_reader;
                GRANT USAGE ON SCHEMA crv_kg TO crv_kg_reader;
                CREATE TABLE crv_kg.vertex_projection(
                    release_id text, iri text, label text, properties jsonb, scope_keys text[]);
                CREATE TABLE crv_kg.edge_projection(
                    release_id text, start_iri text, end_iri text, scope_keys text[]);
                INSERT INTO crv_kg.vertex_projection
                SELECT 'r1', 'urn:doc:' || lpad(i::text, 4, '0'), 'Document',
                    jsonb_build_object('title', 'Report', 'date', NULL,
                        'status', 'observed', 'content_hash', repeat('a', 64)),
                    ARRAY['allowed'] FROM generate_series(1, 405) AS i;
                INSERT INTO crv_kg.vertex_projection
                VALUES ('r1','urn:hidden','Document','{}',ARRAY['hidden']);
            """)
            await connection.execute(migration)
            await connection.execute("SET LOCAL ROLE crv_kg_reader")
            after = None
            seen = []
            for expected in (200, 200, 5, 0):
                result = await connection.execute(
                    "SELECT * FROM crv_kg.list_documents('r1', ARRAY['allowed'], NULL, %s, 200)",
                    (after,),
                )
                page = await result.fetchall()
                assert len(page) == expected
                seen.extend(row[0] for row in page)
                if page:
                    assert all(row[-1] == 405 for row in page)
                    after = page[-1][0]
            assert len(set(seen)) == len(seen) == 405
            assert seen == sorted(seen)
            result = await connection.execute(
                "SELECT * FROM crv_kg.list_documents('r1', ARRAY['unknown'])",
            )
            assert await result.fetchall() == []
            result = await connection.execute(
                "SELECT * FROM crv_kg.list_documents('other', ARRAY['allowed'])",
            )
            assert await result.fetchall() == []
            result = await connection.execute(
                "SELECT has_table_privilege(current_user, 'crv_kg.vertex_projection', 'SELECT')",
            )
            assert await result.fetchone() == (False,)
            # Leave no schema or role in an explicitly supplied test database.
            raise Rollback
