"""PostgreSQL ``SnapshotRepository``: immutable, idempotent source observations."""

from __future__ import annotations

from ...domain.errors import IntegrityError
from ...domain.models import SourceSnapshot
from .pool import PgPool

_FIELDS = tuple(SourceSnapshot.model_fields)
_SELECT = f"SELECT {', '.join(_FIELDS)} FROM ingest.source_snapshot WHERE snapshot_id = $1"
_INSERT = f"""
INSERT INTO ingest.source_snapshot ({", ".join(_FIELDS)})
VALUES ({", ".join(f"${index}" for index in range(1, len(_FIELDS) + 1))})
ON CONFLICT (snapshot_id) DO NOTHING
"""
_LATEST_FOR_PLACEMENT = f"""
SELECT {", ".join(_FIELDS)} FROM ingest.source_snapshot
WHERE placement_id = $1
ORDER BY observed_at DESC, snapshot_id DESC
LIMIT 1
"""
_LATEST_FOR_CONTENT = f"""
SELECT DISTINCT ON (placement_id) {", ".join(_FIELDS)} FROM ingest.source_snapshot
WHERE placement_id IN (
    SELECT placement_id FROM ingest.source_snapshot WHERE content_hash = $1
)
ORDER BY placement_id, observed_at DESC, snapshot_id DESC
"""


class PostgresSnapshots:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def record(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        values = [
            snapshot.provenance.model_dump(mode="json")
            if field == "provenance" and snapshot.provenance else getattr(snapshot, field)
            for field in _FIELDS
        ]
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(_INSERT, *values)
            row = await connection.fetchrow(_SELECT, snapshot.snapshot_id)
        stored = SourceSnapshot.model_validate(dict(row))
        if stored.model_dump(exclude={"observed_at"}) != snapshot.model_dump(
            exclude={"observed_at"}
        ):
            raise IntegrityError(f"Snapshot {snapshot.snapshot_id} conflicts with stored facts")
        return stored

    async def get(self, snapshot_id: str) -> SourceSnapshot | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(_SELECT, snapshot_id)
        return None if row is None else SourceSnapshot.model_validate(dict(row))

    async def latest_for_placement(self, placement_id: str) -> SourceSnapshot | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(_LATEST_FOR_PLACEMENT, placement_id)
        return None if row is None else SourceSnapshot.model_validate(dict(row))

    async def for_content(self, content_hash: str) -> list[SourceSnapshot]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(_LATEST_FOR_CONTENT, content_hash)
        latest = [SourceSnapshot.model_validate(dict(row)) for row in rows]
        return [snapshot for snapshot in latest if snapshot.content_hash == content_hash]

    async def contents_for_placement(self, placement_id: str) -> list[str]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT DISTINCT content_hash FROM ingest.source_snapshot"
                " WHERE placement_id = $1 ORDER BY content_hash",
                placement_id,
            )
        return [row["content_hash"] for row in rows]

    async def contents_with_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """Distinct contents last observed under a blob-name prefix, with one name each."""

        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT content_hash, min(blob_name) AS blob_name FROM ingest.source_snapshot"
                " WHERE blob_name LIKE $1 GROUP BY content_hash ORDER BY content_hash",
                pattern,
            )
        return [(row["content_hash"], row["blob_name"]) for row in rows]
