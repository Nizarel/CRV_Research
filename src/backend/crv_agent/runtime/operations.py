"""Explicit operational commands: memory grants and least-privilege readiness checks."""

import re
from typing import Any, LiteralString

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.runtime.infrastructure import infrastructure
from crv_agent.runtime.settings import Settings

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
MEMORY_TABLES = (
    "crv_threads", "crv_runs", "crv_feedback",
    "checkpoints", "checkpoint_blobs", "checkpoint_writes",
)
_LOGIN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")


async def grant_memory_runtime(
    connection: AsyncConnection[Any], login: str, database: str,
) -> dict[str, object]:
    """Grant DML on agent-memory tables; run as the migration identity after setup-memory."""
    if not _LOGIN.match(login):
        raise ValueError(f"Refusing login name {login!r}")
    result = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,))
    if await result.fetchone() is None:
        raise ValueError(f"Role {login} does not exist; create its Entra login first")
    role = sql.Identifier(login)
    async with connection.transaction():
        await connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(database), role,
        ))
        await connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        await connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
                sql.SQL(", ").join(sql.Identifier("public", t) for t in MEMORY_TABLES), role,
            ),
        )
    result = await connection.execute(
        "SELECT has_schema_privilege(%s, 'public', 'CREATE'),"
        " has_database_privilege(%s, %s, 'CREATE')",
        (login, login, database),
    )
    row = await result.fetchone()
    if row is None or any(row.values() if isinstance(row, dict) else row):
        raise PermissionError(f"{login} can create objects; revoke CREATE before serving")
    return {"login": login, "database": database, "tables": list(MEMORY_TABLES)}


async def memory_ready(pool: Pool) -> None:
    async with pool.connection() as connection:
        for table in MEMORY_TABLES:
            await connection.execute(
                sql.SQL("SELECT 1 FROM {} LIMIT 0").format(sql.Identifier("public", table)),
            )


async def denied(connection: AsyncConnection[Any], statement: LiteralString) -> bool:
    """True when the statement is refused for privileges; never commits a probe."""
    try:
        async with connection.transaction(force_rollback=True):
            await connection.execute(statement)
    except errors.InsufficientPrivilege:
        return True
    except errors.ReadOnlySqlTransaction:
        return True
    return False


async def run_checks(settings: Settings, *, models: bool) -> dict[str, object]:
    report: dict[str, object] = {}
    async with infrastructure(settings) as infra:
        await infra.search.ready()
        report["search_release"] = settings.search_release
        await infra.knowledge.ready(settings.kg_release)
        page = await infra.knowledge.documents([], settings.kg_release, None)
        report["kg_documents"] = page.total
        assert infra.memory_pool is not None
        await memory_ready(infra.memory_pool)
        async with infra.kg_pool.connection() as connection:
            report["kg_table_select_denied"] = await denied(
                connection, "SELECT 1 FROM crv_kg.vertex_projection LIMIT 1",
            )
        async with infra.memory_pool.connection() as connection:
            report["memory_ddl_denied"] = await denied(
                connection, "CREATE TABLE public.crv_permission_probe (id integer)",
            )
        if models:
            pinged: dict[str, bool] = {}
            for chat_model in infra.models.values():
                if chat_model.model_name in pinged:
                    continue
                reply = await chat_model.ainvoke("Reply with OK.")
                pinged[chat_model.model_name] = bool(reply.content)
            report["models"] = pinged
    failures = [
        key for key in ("kg_table_select_denied", "memory_ddl_denied") if report[key] is not True
    ]
    if report["kg_documents"] == 0:
        failures.append("kg_documents")
    report["failures"] = failures
    return report
