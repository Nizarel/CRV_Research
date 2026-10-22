"""Explicit operational commands: memory grants and least-privilege readiness checks."""

import re
import time
from typing import Any, LiteralString, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from crv_agent.domain.answers import ComposedAnswer, Verdict
from crv_agent.domain.evidence import WorkerResult
from crv_agent.domain.planning import QueryPlan
from crv_agent.orchestration.extraction import Extraction
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


PROBE_RULES = (
    "Connectivity probe. Return the smallest valid object for the schema: empty lists, null "
    "where allowed, false for booleans, intent 'lookup', language 'en', short strings."
)


async def probe_model(chat_model: ChatOpenAI, schema: type[BaseModel]) -> dict[str, object]:
    """One strict structured-output call with the role's real schema and reasoning effort."""
    report: dict[str, object] = {
        "deployment": chat_model.model_name, "reasoning_effort": chat_model.reasoning_effort,
        "schema": schema.__name__,
    }
    started = time.monotonic()
    try:
        result = cast(dict[str, Any], await chat_model.with_structured_output(
            schema, method="json_schema", strict=True, include_raw=True,
        ).ainvoke([SystemMessage(content=PROBE_RULES), HumanMessage(content="Probe.")]))
    except Exception as error:
        return {**report, "ok": False, "error": f"{type(error).__name__}: {error}"[:300]}
    raw = result.get("raw")
    usage = raw.usage_metadata if isinstance(raw, AIMessage) else None
    report.update(
        ok=result.get("parsed") is not None, seconds=round(time.monotonic() - started, 3),
        served_model=raw.response_metadata.get("model_name") if isinstance(raw, AIMessage)
        else None,
        reasoning_tokens=usage.get("output_token_details", {}).get("reasoning") if usage else None,
    )
    if not report["ok"]:
        report["error"] = str(result.get("parsing_error"))[:300]
    return report


async def run_checks(settings: Settings, *, models: bool) -> dict[str, object]:
    report: dict[str, object] = {}
    probes: dict[str, dict[str, object]] = {}
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
            schemas: dict[str, type[BaseModel]] = {
                "planner": QueryPlan, "answer": ComposedAnswer, "verifier": Verdict,
                "worker": Extraction if settings.worker_mode == "extract" else WorkerResult,
            }
            probes = {
                role: await probe_model(chat_model, schemas[role])
                for role, chat_model in infra.models.items()
            }
            report["models"] = probes
    failures = [
        key for key in ("kg_table_select_denied", "memory_ddl_denied") if report[key] is not True
    ]
    if report["kg_documents"] == 0:
        failures.append("kg_documents")
    failures.extend(f"model:{role}" for role, probe in probes.items() if not probe["ok"])
    report["failures"] = failures
    return report
