from importlib.resources import files

from azure.identity.aio import DefaultAzureCredential

from crv_agent.adapters.postgres.memory import checkpointer, entra_pool
from crv_agent.runtime.operations import grant_memory_runtime
from crv_agent.runtime.settings import Settings


async def setup_memory(settings: Settings) -> None:
    """Run explicitly using the migration identity, never from the API lifespan."""
    async with DefaultAzureCredential() as credential, entra_pool(
        credential, settings.postgres_host, settings.memory_database, settings.memory_user,
        read_only=False,
        root_cert=settings.postgres_root_cert,
    ) as pool:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                files("crv_agent.adapters.postgres").joinpath("schema.sql").read_text(),
            )
        await checkpointer(pool).setup()


async def grant_memory(settings: Settings, login: str) -> dict[str, object]:
    """Run as the migration identity after setup_memory; grants DML only."""
    async with DefaultAzureCredential() as credential, entra_pool(
        credential, settings.postgres_host, settings.memory_database, settings.memory_user,
        read_only=False,
        root_cert=settings.postgres_root_cert,
    ) as pool, pool.connection() as conn:
        return await grant_memory_runtime(conn, login, settings.memory_database)
