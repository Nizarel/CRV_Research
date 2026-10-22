import os
import ssl
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
_SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Azure Linux
)


def resolve_root_cert(value: str) -> str:
    """Concrete CA bundle for libpq.

    psycopg-binary bundles its own libpq/OpenSSL, whose built-in "system" store may not be the
    OS store, so verification would fail. Use the OS bundle file when one exists; verify-full
    still checks the chain and host name.
    """
    if value != "system":
        return value
    candidates = [ssl.get_default_verify_paths().cafile, *_SYSTEM_BUNDLES]
    return next((path for path in candidates if path and os.path.isfile(path)), value)


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
            sslmode="verify-full", sslrootcert=resolve_root_cert(root_cert),
            connect_timeout=10,
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
