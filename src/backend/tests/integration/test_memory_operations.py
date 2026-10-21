from importlib.resources import files
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.postgres.memory import checkpointer
from crv_agent.runtime.operations import MEMORY_TABLES, denied, grant_memory_runtime

pytestmark = pytest.mark.integration


async def test_memory_runtime_grants_are_dml_only(pg_dsn: str) -> None:
    memory_database = "crv_mem_" + uuid4().hex[:12]
    login = "id-crv-agent-" + uuid4().hex[:8]
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{memory_database}"')
        await admin.execute(f'CREATE ROLE "{login}" NOLOGIN')
    dsn = make_conninfo(**{**conninfo_to_dict(pg_dsn), "dbname": memory_database})
    try:
        async with AsyncConnectionPool(
            dsn, kwargs={"autocommit": True, "row_factory": dict_row}, open=False,
        ) as pool:
            async with pool.connection() as connection:
                await connection.execute(
                    files("crv_agent.adapters.postgres").joinpath("schema.sql").read_text(),
                )
            await checkpointer(pool).setup()
            async with pool.connection() as connection:
                with pytest.raises(ValueError, match="does not exist"):
                    await grant_memory_runtime(connection, "id-missing-role", memory_database)
                with pytest.raises(ValueError, match="Refusing"):
                    await grant_memory_runtime(connection, 'x"; drop', memory_database)
                report = await grant_memory_runtime(connection, login, memory_database)
                assert report["tables"] == list(MEMORY_TABLES)
                # Idempotent re-run.
                await grant_memory_runtime(connection, login, memory_database)
                await connection.execute(f'SET ROLE "{login}"')
                await connection.execute(
                    "INSERT INTO crv_threads (id, owner, search_release, kg_release)"
                    " VALUES ('t1', 'o', 's', 'k')",
                )
                await connection.execute("DELETE FROM crv_threads WHERE id = 't1'")
                await connection.execute("SELECT 1 FROM checkpoints LIMIT 0")
                assert await denied(connection, "CREATE TABLE public.probe (id integer)")
                assert await denied(connection, "DROP TABLE crv_threads")
                assert await denied(connection, "SELECT 1 FROM checkpoint_migrations")
                assert not await denied(connection, "SELECT 1 FROM crv_runs LIMIT 0")
                with pytest.raises(errors.InsufficientPrivilege):
                    await connection.execute("CREATE SCHEMA probe")
                await connection.execute("RESET ROLE")
    finally:
        async with await AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{memory_database}" WITH (FORCE)')
            await admin.execute(f'DROP ROLE IF EXISTS "{login}"')
