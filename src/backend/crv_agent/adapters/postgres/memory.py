from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from azure.identity.aio import DefaultAzureCredential
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from crv_agent.domain import answers, evidence, planning

DB_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def serializer() -> JsonPlusSerializer:
    permitted = [
        (model.__module__, model.__name__)
        for model in (
            planning.QueryPlan, planning.Facet, answers.Coverage, answers.Conflict,
            answers.Answer, evidence.Claim, evidence.Evidence, evidence.Calculation,
        )
    ]
    return JsonPlusSerializer(
        pickle_fallback=False, allowed_msgpack_modules=permitted, allowed_json_modules=permitted,
    )


@asynccontextmanager
async def entra_pool(
    credential: DefaultAzureCredential, host: str, database: str, user: str, *, read_only: bool,
    root_cert: str = "system",
) -> AsyncIterator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    async def conninfo() -> str:
        token = await credential.get_token(DB_SCOPE)
        return make_conninfo(
            host=host, dbname=database, user=user, password=token.token,
            sslmode="verify-full", sslrootcert=root_cert, connect_timeout=10,
        )

    async def configure(conn: AsyncConnection[DictRow]) -> None:
        await conn.execute("SET statement_timeout = '15s'")
        if read_only:
            await conn.execute("SET ROLE crv_kg_reader")
            await conn.execute("SET default_transaction_read_only = on")

    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=conninfo, kwargs={"row_factory": dict_row, "autocommit": True},
        min_size=1, max_size=5, max_lifetime=2700, timeout=15, open=False,
        configure=configure,
    )
    await pool.open()
    try:
        await pool.wait(timeout=15)
        yield pool
    finally:
        await pool.close()


def checkpointer(
    pool: AsyncConnectionPool[AsyncConnection[DictRow]],
) -> AsyncPostgresSaver:
    return AsyncPostgresSaver(pool, serde=serializer())
