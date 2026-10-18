"""The agent's kg login: execute-only, with pinned session defaults."""

from __future__ import annotations

from typing import Any

import pytest

from ingest_pipeline.adapters.postgres.kg_access import ensure_entra_login, grant_reader
from ingest_pipeline.domain.errors import InvalidInputError

pytestmark = pytest.mark.integration

LOGIN = "test-kg-login"


async def test_grant_reader_makes_an_execute_only_login(pg_pool: Any) -> None:
    async with pg_pool.acquire() as connection:
        await connection.execute(f'CREATE ROLE "{LOGIN}" LOGIN')
        try:
            first = await grant_reader(connection, LOGIN, statement_timeout_ms=2000)
            again = await grant_reader(connection, LOGIN, statement_timeout_ms=2000)
            settings = await connection.fetchval(
                "SELECT setconfig FROM pg_db_role_setting"
                " WHERE setrole = (SELECT oid FROM pg_roles WHERE rolname = $1)"
                " AND setdatabase = (SELECT oid FROM pg_database"
                " WHERE datname = current_database())",
                LOGIN,
            )
        finally:
            await connection.execute(f'DROP OWNED BY "{LOGIN}"')
            await connection.execute(f'DROP ROLE "{LOGIN}"')
    assert first == again
    assert first["session_defaults"] == {"role": True, "read_only": True, "statement_timeout": True}
    assert (first["can_read_context"], first["can_select_tables"], first["elevated"]) == (
        True,
        False,
        False,
    )
    assert sorted(settings) == [
        "default_transaction_read_only=on",
        "role=tizert_kg_reader",
        "statement_timeout=2000",
    ]


@pytest.mark.parametrize(
    ("login", "object_id"),
    [
        ('x"; DROP ROLE postgres; --', "00000000-0000-0000-0000-000000000000"),
        ("id-agentzmaint-kg-reader", "not-a-guid"),
    ],
)
async def test_unsafe_names_are_refused_before_any_query(login: str, object_id: str) -> None:
    with pytest.raises(InvalidInputError):
        await ensure_entra_login(object(), login, object_id)  # type: ignore[arg-type]
