"""Ordered, checksum-verified SQL migrations for the ``ingestion-catalog`` database.

Versions up to ``0002_knowledge_prerequisites`` are an external baseline applied by
``infra/sql/ingestion-catalog``. This runner owns ``0003`` onwards, shipped as package data.
Checksums hash the SQL with LF line endings so Windows and Linux checkouts agree.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable

from ...domain.errors import ConfigurationError, IntegrityError
from .pool import PgConnection

BASELINE_VERSION = "0002_knowledge_prerequisites"
LOCK_KEY = "tizert-ingest-migrations"
_NAME = re.compile(r"^(?P<version>\d{4}_[a-z0-9_]+)\.sql$")


def _number(version: str) -> int:
    return int(version[:4])


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationReport:
    dry_run: bool
    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    unknown_applied: list[str] = field(default_factory=list)


def normalize_sql(text: str) -> str:
    return text.replace("\r\n", "\n")


def load_migrations(directory: Traversable | None = None) -> list[Migration]:
    root = directory or resources.files("tizertmaint_pipeline").joinpath("sql")
    migrations: list[Migration] = []
    for entry in root.iterdir():
        match = _NAME.match(entry.name)
        if match is None:
            continue
        migrations.append(Migration(match["version"], normalize_sql(entry.read_text("utf-8"))))
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len({_number(version) for version in versions}) != len(versions):
        raise ConfigurationError("Duplicate migration numbers")
    if any(_number(version) <= _number(BASELINE_VERSION) for version in versions):
        raise ConfigurationError(f"Packaged migrations must be numbered after {BASELINE_VERSION}")
    return migrations


class _DryRunRollback(Exception):
    pass


async def apply_migrations(
    connection: PgConnection, migrations: list[Migration], *, dry_run: bool
) -> MigrationReport:
    """Apply pending migrations in one transaction under an advisory lock.

    A checksum mismatch for an applied version aborts everything: published migrations are
    immutable, so changes need a new version.
    """

    report = MigrationReport(dry_run=dry_run)
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", LOCK_KEY)
            if await connection.fetchval("SELECT to_regclass('ingest.schema_migration')") is None:
                raise ConfigurationError(f"Apply {BASELINE_VERSION} before pipeline migrations")
            rows = await connection.fetch("SELECT version, sha256 FROM ingest.schema_migration")
            applied = {row["version"]: row["sha256"] for row in rows}
            if BASELINE_VERSION not in applied:
                raise ConfigurationError(f"Baseline {BASELINE_VERSION} is not registered")

            known = {migration.version for migration in migrations}
            report.unknown_applied.extend(
                sorted(
                    version
                    for version in applied
                    if _number(version) > _number(BASELINE_VERSION) and version not in known
                )
            )
            for migration in migrations:
                recorded = applied.get(migration.version)
                if recorded is not None:
                    if recorded != migration.sha256:
                        raise IntegrityError(
                            f"Migration {migration.version} changed after it was applied"
                        )
                    report.already_applied.append(migration.version)
                    continue
                await connection.execute(migration.sql)
                await connection.execute(
                    "INSERT INTO ingest.schema_migration (version, sha256) VALUES ($1, $2)",
                    migration.version,
                    migration.sha256,
                )
                report.applied.append(migration.version)
            if dry_run:
                raise _DryRunRollback
    except _DryRunRollback:
        pass
    return report
