"""Edge memory and logins against a real PostgreSQL 16: edge-logins.sql, then setup-memory and
grant-memory-runtime with password auth, then the runtime pools as crv_edge_agent."""

import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from crv_agent.adapters.postgres.memory import pg_pool
from crv_agent.runtime.database import kg_profile, memory_profile
from crv_agent.runtime.migrations import grant_memory, setup_memory
from crv_agent.runtime.operations import memory_ready

pytestmark = pytest.mark.integration
SQL = Path(__file__).resolve().parents[4] / "infra" / "sql" / "migration" / "edge-logins.sql"


@pytest.fixture(scope="module")
def server() -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required")
    name = f"crv-edge-memory-{secrets.token_hex(4)}"
    admin = secrets.token_urlsafe(16)
    subprocess.run([
        "docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432",
        "-e", f"POSTGRES_PASSWORD={admin}", "postgres:16",
    ], check=True, capture_output=True)
    try:
        port = int(subprocess.run(
            ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True,
        ).stdout.strip().rsplit(":", 1)[1])
        subprocess.run(
            ["docker", "exec", name, "sh", "-c",
             "for i in $(seq 60); do pg_isready -h 127.0.0.1 -U postgres && exit 0; sleep 1; "
             "done; exit 1"], check=True, capture_output=True,
        )
        yield {"name": name, "port": port, "admin": admin}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def admin(server: dict[str, Any], database: str = "postgres") -> psycopg.Connection[Any]:
    return psycopg.connect(
        host="127.0.0.1", port=server["port"], user="postgres", password=server["admin"],
        dbname=database, autocommit=True,
    )


def run_edge_logins(server: dict[str, Any], agent: str, migrator: str) -> str:
    subprocess.run(["docker", "cp", str(SQL), f"{server['name']}:/tmp/edge-logins.sql"],
                   check=True, capture_output=True)
    result = subprocess.run(
        ["docker", "exec", "-i", "-e", "CRV_AGENT_PASSWORD", "-e", "CRV_MIGRATOR_PASSWORD",
         server["name"], "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
         "-d", "postgres", "-f", "/tmp/edge-logins.sql"],
        capture_output=True, text=True,
        env={**os.environ, "CRV_AGENT_PASSWORD": agent, "CRV_MIGRATOR_PASSWORD": migrator},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def settings(server: dict[str, Any], tmp_path: Path, user: str, password: str) -> Any:
    from crv_agent.runtime.settings import Settings

    secret = tmp_path / f"{user}.password"
    secret.write_text(password + "\n", encoding="utf-8")
    return Settings(
        kg_release="kg1", postgres_host="127.0.0.1", postgres_port=server["port"],
        postgres_auth="password", postgres_sslmode="disable",
        postgres_password_file=str(secret), postgres_user="crv_edge_agent",
        kg_database="ingestion-catalog", memory_user=user, model_endpoint="http://127.0.0.1/v1",
        model_auth="none", search_backend="postgres", pg_vector_model="m3",
        pg_embedder="openai", pg_embedding_url="http://127.0.0.1:1/v1",
        pg_embedding_model="bge-m3", planner_deployment="p", worker_deployment="w",
        answer_deployment="a", verifier_deployment="v", local_owner="edge",
    )


@pytest.mark.asyncio
async def test_edge_logins_memory_setup_and_runtime_pools(
    server: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in [key for key in os.environ if key.upper().startswith("CRV_")]:
        monkeypatch.delenv(key)
    with admin(server) as connection:
        connection.execute("CREATE ROLE catalog_owner NOLOGIN")
        connection.execute("CREATE ROLE crv_kg_reader NOLOGIN")
        connection.execute('CREATE DATABASE "ingestion-catalog" OWNER catalog_owner')
    with admin(server, "ingestion-catalog") as connection:
        connection.execute("CREATE SCHEMA kg AUTHORIZATION catalog_owner")
        connection.execute("CREATE TABLE kg.facts (id int)")
        connection.execute("GRANT USAGE ON SCHEMA kg TO crv_kg_reader")
        connection.execute("GRANT SELECT ON kg.facts TO crv_kg_reader")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    agent, migrator = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    summary = run_edge_logins(server, agent, migrator)
    assert '"memory_owner" : "crv_edge_migrator"' in summary
    assert agent not in summary and migrator not in summary
    # Idempotent: a second run keeps everything and rotates nothing unexpectedly.
    run_edge_logins(server, agent, migrator)

    as_migrator = settings(server, tmp_path, "crv_edge_migrator", migrator)
    await setup_memory(as_migrator)
    report = await grant_memory(as_migrator, "crv_edge_agent")
    assert report["login"] == "crv_edge_agent"

    runtime = settings(server, tmp_path, "crv_edge_agent", agent)
    async with pg_pool(memory_profile(runtime), None, read_only=False) as memory:
        await memory_ready(memory)
        async with memory.connection() as connection:
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute("CREATE TABLE public.crv_probe (id int)")
    async with pg_pool(kg_profile(runtime), None, read_only=True) as kg:
        async with kg.connection() as connection:
            row = await (await connection.execute("SELECT current_user AS who")).fetchone()
            assert row == {"who": "crv_kg_reader"}
            await connection.execute("SELECT count(*) FROM kg.facts")
            with pytest.raises(errors.ReadOnlySqlTransaction):
                await connection.execute("INSERT INTO kg.facts VALUES (1)")
    with admin(server) as connection:
        member = connection.execute(
            "SELECT pg_has_role('crv_edge_agent', 'catalog_owner', 'MEMBER')",
        ).fetchone()
        assert member == (False,)
