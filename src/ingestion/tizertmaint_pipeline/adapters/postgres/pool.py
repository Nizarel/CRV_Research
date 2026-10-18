"""asyncpg pool with per-connection Entra tokens, verified TLS and JSON codecs."""

from __future__ import annotations

import json
import ssl
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from ...config import DatabaseSettings, require
from ...domain.errors import ConfigurationError

POSTGRES_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


class PgConnection(Protocol):
    async def execute(self, query: str, *args: Any) -> str: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    def transaction(self) -> AbstractAsyncContextManager[Any]: ...


class PgPool(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[PgConnection]: ...


class AsyncTokenCredential(Protocol):
    async def get_token(self, *scopes: str) -> Any: ...


async def init_connection(connection: Any) -> None:
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


def _pool_options(settings: DatabaseSettings) -> dict[str, Any]:
    return {
        "min_size": settings.min_pool_size,
        "max_size": settings.max_pool_size,
        "command_timeout": settings.command_timeout_seconds,
        "server_settings": {
            "application_name": settings.application_name,
            "statement_timeout": str(settings.statement_timeout_ms),
        },
        "init": init_connection,
    }


async def create_pool(
    settings: DatabaseSettings, credential: AsyncTokenCredential | None = None
) -> Any:
    """Create a pool; each new connection fetches a fresh Entra token (tokens expire)."""

    import asyncpg

    if settings.dsn is not None:
        return await asyncpg.create_pool(
            dsn=settings.dsn.get_secret_value(), **_pool_options(settings)
        )
    if credential is None:
        raise ConfigurationError("An Entra credential is required when TIZ_DB__DSN is not set")
    host = require(settings.host, "TIZ_DB__HOST")
    user = require(settings.user, "TIZ_DB__USER")

    async def entra_password() -> str:
        token = await credential.get_token(POSTGRES_ENTRA_SCOPE)
        return str(token.token)

    return await asyncpg.create_pool(
        host=host,
        port=settings.port,
        user=user,
        database=settings.database,
        password=entra_password,
        ssl=ssl.create_default_context(),
        **_pool_options(settings),
    )
