from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.resources import files

from azure.identity.aio import DefaultAzureCredential
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.postgres.memory import checkpointer, pg_pool
from crv_agent.runtime.database import memory_profile
from crv_agent.runtime.operations import grant_memory_runtime
from crv_agent.runtime.settings import Settings


@asynccontextmanager
async def _memory_admin_pool(
    settings: Settings,
) -> AsyncIterator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    profile = memory_profile(settings)
    async with AsyncExitStack() as stack:
        credential = (
            await stack.enter_async_context(DefaultAzureCredential())
            if profile.auth == "entra" else None
        )
        yield await stack.enter_async_context(pg_pool(profile, credential, read_only=False))


async def setup_memory(settings: Settings) -> None:
    """Run explicitly using the migration identity, never from the API lifespan."""
    async with _memory_admin_pool(settings) as pool:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                files("crv_agent.adapters.postgres").joinpath("schema.sql").read_text(),
            )
        await checkpointer(pool).setup()


async def grant_memory(settings: Settings, login: str) -> dict[str, object]:
    """Run as the migration identity after setup_memory; grants DML only."""
    async with _memory_admin_pool(settings) as pool, pool.connection() as conn:
        return await grant_memory_runtime(conn, login, settings.memory_database)
