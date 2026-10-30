"""PostgreSQL connection profiles from settings (kept out of settings.py, which stays
importable on its own)."""

from crv_agent.adapters.postgres.memory import PgProfile
from crv_agent.runtime.settings import Settings


def kg_profile(settings: Settings) -> PgProfile:
    """Read-only KG and retrieval login (``crv_kg_reader``)."""
    password = settings.postgres_auth == "password"
    return PgProfile(
        host=settings.postgres_host, database=settings.kg_database, user=settings.postgres_user,
        auth=settings.postgres_auth, port=settings.postgres_port,
        sslmode=settings.postgres_sslmode, root_cert=settings.postgres_root_cert,
        password_file=settings.postgres_password_file if password else None,
    )


def memory_profile(settings: Settings) -> PgProfile:
    s = settings
    auth = s.memory_auth if s.memory_auth is not None else s.postgres_auth
    return PgProfile(
        host=s.memory_host if s.memory_host is not None else s.postgres_host,
        database=s.memory_database, user=s.memory_user, auth=auth,
        port=s.memory_port if s.memory_port is not None else s.postgres_port,
        sslmode=s.memory_sslmode if s.memory_sslmode is not None else s.postgres_sslmode,
        root_cert=s.memory_root_cert if s.memory_root_cert is not None else s.postgres_root_cert,
        password_file=None if auth == "entra" else (
            s.memory_password_file if s.memory_password_file is not None
            else s.postgres_password_file
        ),
    )
