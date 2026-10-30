import asyncio
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from azure.core.credentials_async import AsyncTokenCredential
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.secrets import read_secret_file
from crv_agent.domain import answers, evidence, planning

DB_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
PgAuth = Literal["entra", "password"]
SslMode = Literal["disable", "prefer", "require", "verify-ca", "verify-full"]
_VERIFYING = frozenset({"verify-ca", "verify-full"})
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
            answers.Answer, answers.RoleUsage, evidence.Claim, evidence.Evidence,
            evidence.Calculation,
        )
    ]
    return JsonPlusSerializer(
        pickle_fallback=False, allowed_msgpack_modules=permitted, allowed_json_modules=permitted,
    )


@dataclass(frozen=True)
class PgProfile:
    """Where and how to log in. Entra uses a fresh token per connection; password auth reads
    ``password_file`` per connection, so a rotated password needs no restart."""

    host: str
    database: str
    user: str
    auth: PgAuth = "entra"
    port: int | None = None
    sslmode: SslMode = "verify-full"
    root_cert: str = "system"
    password_file: str | None = None

    def __post_init__(self) -> None:
        if self.auth == "password" and not self.password_file:
            raise ValueError("PostgreSQL password auth needs a password file")
        if self.auth == "entra" and self.sslmode != "verify-full":
            raise ValueError("Entra PostgreSQL logins require sslmode verify-full")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("PostgreSQL port must be between 1 and 65535")


def read_password(path: str) -> str:
    return read_secret_file(path, "PostgreSQL password")


async def conninfo(profile: PgProfile, credential: AsyncTokenCredential | None) -> str:
    if profile.auth == "entra":
        if credential is None:
            raise ValueError("An Entra PostgreSQL login needs an Azure credential")
        password = (await credential.get_token(DB_SCOPE)).token
    else:
        assert profile.password_file is not None
        password = await asyncio.to_thread(read_password, profile.password_file)
    options: dict[str, str | int] = {
        "host": profile.host, "dbname": profile.database, "user": profile.user,
        "password": password, "sslmode": profile.sslmode, "connect_timeout": 10,
    }
    if profile.port is not None:
        options["port"] = profile.port
    # Without verification a CA file would silently upgrade "require" to verify-ca.
    if profile.sslmode in _VERIFYING:
        options["sslrootcert"] = resolve_root_cert(profile.root_cert)
    return make_conninfo("", **options)


@asynccontextmanager
async def pg_pool(
    profile: PgProfile, credential: AsyncTokenCredential | None, *, read_only: bool,
    max_size: int = 5,
) -> AsyncIterator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    if profile.auth == "entra" and credential is None:
        raise ValueError("An Entra PostgreSQL login needs an Azure credential")

    async def connection_info() -> str:
        return await conninfo(profile, credential)

    async def configure(conn: AsyncConnection[DictRow]) -> None:
        await conn.execute("SET statement_timeout = '15s'")
        if read_only:
            await conn.execute("SET ROLE crv_kg_reader")
            await conn.execute("SET default_transaction_read_only = on")

    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=connection_info, kwargs={"row_factory": dict_row, "autocommit": True},
        min_size=1, max_size=max_size, max_lifetime=2700, timeout=15, open=False,
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
