import os
import secrets
from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    if configured := os.environ.get("CRV_TEST_PG_DSN"):
        yield configured
        return
    with PostgresContainer(
        "postgres:16", username="crv_test", password=secrets.token_urlsafe(24),
        dbname="crv_agent_test", driver=None,
    ) as database:
        yield database.get_connection_url()
