"""Database access for the agent's knowledge-graph login: an Entra principal that can do nothing
in the catalog but execute the kg read functions (``tizert_kg_reader``, migrations 0012-0013).

Names are quoted server-side with ``format('%I')`` after a pattern check; values are bound.
"""

from __future__ import annotations

import re
from typing import Any

from ...domain.errors import IntegrityError, InvalidInputError
from .pool import PgConnection

READER_ROLE = "tizert_kg_reader"
_LOGIN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _check_login(login: str) -> None:
    if not _LOGIN.match(login):
        raise InvalidInputError(f"Refusing login name {login!r}")


async def ensure_entra_login(connection: PgConnection, login: str, object_id: str) -> bool:
    """Create the Entra service login once; run in the ``postgres`` database.

    Returns whether it was created. An existing role with another (or no) Entra mapping is
    refused rather than repurposed.
    """

    _check_login(login)
    if not _OBJECT_ID.match(object_id):
        raise InvalidInputError(f"Refusing object ID {object_id!r}")
    exists = await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", login)
    label = await connection.fetchval(
        "SELECT label FROM pg_shseclabel WHERE provider = 'pgaadauth'"
        " AND objoid = (SELECT oid FROM pg_roles WHERE rolname = $1)",
        login,
    )
    if exists:
        if label != f"aadauth,oid={object_id},type=service":
            raise IntegrityError(f"Role {login} exists with Entra mapping {label!r}")
        return False
    await connection.execute(
        "SELECT pgaadauth_create_principal_with_oid($1, $2, 'service', false, false)",
        login,
        object_id,
    )
    return True


async def grant_reader(
    connection: PgConnection, login: str, *, statement_timeout_ms: int
) -> dict[str, Any]:
    """Grant ``login`` the reader role in the current database and pin its session defaults.

    Session defaults (start as the reader role, read-only, statement timeout) need ADMIN on the
    login; where the server withholds it they are reported as not set, and the client must set
    them per session.
    """

    _check_login(login)
    if not 100 <= statement_timeout_ms <= 60_000:
        raise InvalidInputError("statement_timeout_ms must be between 100 and 60000")
    database = await connection.fetchval("SELECT current_database()")

    async def run(template: str, value: str | None = None) -> None:
        statement = await connection.fetchval(
            "SELECT format($1::text, $2::text, $3::text, $4::text)",
            template,
            login,
            database,
            value,
        )
        await connection.execute(statement)

    async with connection.transaction():
        await run("GRANT CONNECT ON DATABASE %2$I TO %1$I")
        await run(f"GRANT {READER_ROLE} TO %1$I")
    settings = {
        "role": (f"SET role = {READER_ROLE}", None),
        "read_only": ("SET default_transaction_read_only = on", None),
        "statement_timeout": ("SET statement_timeout = %3$L", str(statement_timeout_ms)),
    }
    defaults: dict[str, bool] = {}
    for name, (clause, value) in settings.items():
        try:
            async with connection.transaction():
                await run(f"ALTER ROLE %1$I IN DATABASE %2$I {clause}", value)
            defaults[name] = True
        except Exception as error:
            if getattr(error, "sqlstate", None) != "42501":  # insufficient_privilege
                raise
            defaults[name] = False
    checks = await connection.fetchrow(
        "SELECT has_function_privilege($1, 'kg.context(text,text,text[],integer,integer)',"
        " 'EXECUTE') AS can_read_context,"
        " has_table_privilege($1, 'kg.entity', 'SELECT') AS can_select_tables,"
        " (SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = $1)"
        " AS elevated",
        login,
    )
    return {"database": database, "session_defaults": defaults, **dict(checks)}
