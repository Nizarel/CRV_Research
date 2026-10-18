"""Projection writes lock rows in key order, so concurrent jobs on copies cannot deadlock."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from tizertmaint_pipeline.adapters.postgres.evidence import PostgresEvidenceStore
from tizertmaint_pipeline.domain.projection import ProjectionItem, ProjectionState

NOW = datetime(2026, 10, 13, tzinfo=UTC)


class _Connection:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.keys.append(args[2])

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Connection]:
        yield self.connection


def _item(key: str) -> ProjectionItem:
    return ProjectionItem(
        store="search",
        target="maint-evidence-v2",
        projection_id=key,
        release_id="r",
        evidence_id="evd:1",
        chunk_id=f"chunk:{key}",
        content_hash="a" * 64,
        placement_id="p",
        payload_sha256="b" * 64,
    )


async def test_upsert_and_set_state_lock_rows_in_key_order() -> None:
    pool = _Pool()
    store = PostgresEvidenceStore(pool)
    await store.upsert([_item("c"), _item("a"), _item("b")], now=NOW)
    await store.set_state(
        "search",
        "maint-evidence-v2",
        {"b": (ProjectionState.VERIFIED, None), "c": (ProjectionState.VERIFIED, None),
         "a": (ProjectionState.FAILED_PERMANENT, "x")},
        now=NOW,
    )
    assert pool.connection.keys == ["a", "b", "c", "a", "b", "c"]
