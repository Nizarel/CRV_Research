import os
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from crv_agent.adapters.postgres import memory
from crv_agent.adapters.postgres.memory import DB_SCOPE, PgProfile, conninfo, pg_pool

BASE = {
    "search_endpoint": "https://search", "kg_release": "kg1", "postgres_host": "pg",
    "postgres_user": "reader", "kg_database": "ingestion-catalog", "memory_user": "memory",
    "openai_endpoint": "https://openai", "planner_deployment": "p", "worker_deployment": "w",
    "answer_deployment": "a", "verifier_deployment": "v", "local_owner": "dev",
}


class Credential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_token(self, *scopes: str, **kwargs: Any) -> SimpleNamespace:
        self.scopes.extend(scopes)
        return SimpleNamespace(token="entra-token")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.upper().startswith("CRV_")]:
        monkeypatch.delenv(key)


def settings(**overrides: Any) -> Any:
    from crv_agent.runtime.settings import Settings

    return Settings(**{**BASE, **overrides})


@pytest.mark.asyncio
async def test_entra_conninfo_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "resolve_root_cert", lambda value: f"resolved:{value}")
    credential = Credential()
    info = conninfo_to_dict(await conninfo(PgProfile("pg", "db", "reader"), credential))
    assert credential.scopes == [DB_SCOPE]
    assert info == {
        "host": "pg", "dbname": "db", "user": "reader", "password": "entra-token",
        "sslmode": "verify-full", "connect_timeout": "10", "sslrootcert": "resolved:system",
    }


@pytest.mark.asyncio
async def test_password_conninfo_rereads_the_file_and_skips_ca_without_verification(
    tmp_path: Any,
) -> None:
    secret = tmp_path / "pg.password"
    secret.write_text("first\n", encoding="utf-8")
    profile = PgProfile(
        "postgres", "db", "agent", auth="password", port=5433, sslmode="disable",
        password_file=str(secret),
    )
    first = conninfo_to_dict(await conninfo(profile, None))
    assert first["password"] == "first" and first["port"] == "5433"
    assert first["sslmode"] == "disable" and "sslrootcert" not in first
    secret.write_text("rotated", encoding="utf-8")
    assert conninfo_to_dict(await conninfo(profile, None))["password"] == "rotated"
    verified = PgProfile(
        "postgres", "db", "agent", auth="password", sslmode="verify-ca",
        root_cert="/ca.pem", password_file=str(secret),
    )
    assert conninfo_to_dict(await conninfo(verified, None))["sslrootcert"] == "/ca.pem"


@pytest.mark.asyncio
async def test_password_file_errors_do_not_leak_or_accept_blank(tmp_path: Any) -> None:
    blank = tmp_path / "blank"
    blank.write_text("  \n", encoding="utf-8")
    profile = PgProfile("pg", "db", "u", auth="password", password_file=str(blank))
    with pytest.raises(ConnectionError, match="empty"):
        await conninfo(profile, None)
    missing = PgProfile("pg", "db", "u", auth="password", password_file=str(tmp_path / "no"))
    with pytest.raises(ConnectionError, match="Cannot read"):
        await conninfo(missing, None)


def test_profile_rejects_unsafe_combinations() -> None:
    with pytest.raises(ValueError, match="password file"):
        PgProfile("pg", "db", "u", auth="password")
    with pytest.raises(ValueError, match="verify-full"):
        PgProfile("pg", "db", "u", sslmode="require")
    with pytest.raises(ValueError, match="port"):
        PgProfile("pg", "db", "u", port=0)


@pytest.mark.asyncio
async def test_pool_uses_profile_and_keeps_reader_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    secret = tmp_path / "pg.password"
    secret.write_text("pw", encoding="utf-8")
    seen: dict[str, Any] = {}

    class FakePool:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def open(self) -> None:
            seen["info"] = conninfo_to_dict(await seen["conninfo"]())

        async def wait(self, timeout: float) -> None:
            return None

        async def close(self) -> None:
            seen["closed"] = True

    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, statement: str) -> None:
            self.statements.append(statement)

    monkeypatch.setattr(memory, "AsyncConnectionPool", FakePool)
    profile = PgProfile("pg", "db", "u", auth="password", port=5433, password_file=str(secret))
    async with pg_pool(profile, None, read_only=True, max_size=7):
        assert seen["info"]["password"] == "pw" and seen["max_size"] == 7
    assert seen["closed"] is True
    connection = Connection()
    await seen["configure"](connection)
    assert connection.statements == [
        "SET statement_timeout = '15s'", "SET ROLE crv_kg_reader",
        "SET default_transaction_read_only = on",
    ]
    with pytest.raises(ValueError, match="Azure credential"):
        async with pg_pool(PgProfile("pg", "db", "u"), None, read_only=True):
            pass


def test_azure_defaults_share_one_server(clean_env: None) -> None:
    from crv_agent.runtime.database import kg_profile, memory_profile

    current = settings()
    assert kg_profile(current) == PgProfile("pg", "ingestion-catalog", "reader")
    assert memory_profile(current) == PgProfile("pg", "crv-agent-memory", "memory")


def test_edge_password_settings_and_memory_overrides(clean_env: None, tmp_path: Any) -> None:
    from crv_agent.runtime.database import kg_profile, memory_profile

    secret = str(tmp_path / "agent.password")
    current = settings(
        postgres_host="postgres", postgres_port="5432", postgres_auth="password",
        postgres_sslmode="disable", postgres_password_file=secret, memory_host="",
        memory_port="",
    )
    assert kg_profile(current) == PgProfile(
        "postgres", "ingestion-catalog", "reader", auth="password", port=5432,
        sslmode="disable", password_file=secret,
    )
    assert memory_profile(current).host == "postgres"
    assert memory_profile(current).password_file == secret
    split = settings(
        postgres_auth="password", postgres_sslmode="require", postgres_password_file=secret,
        memory_host="memory-host", memory_auth="entra", memory_sslmode="verify-full",
    )
    assert memory_profile(split) == PgProfile("memory-host", "crv-agent-memory", "memory")
    assert current.model_dump()["postgres_password_file"] == secret


def test_settings_reject_weak_tls_for_entra_and_password_without_file(clean_env: None) -> None:
    with pytest.raises(ValueError, match="verify-full"):
        settings(postgres_sslmode="require")
    with pytest.raises(ValueError, match="password file"):
        settings(postgres_auth="password", postgres_sslmode="disable")
    with pytest.raises(ValueError, match="verify-full"):
        settings(
            postgres_auth="password", postgres_password_file="/p", postgres_sslmode="disable",
            memory_auth="entra",
        )
