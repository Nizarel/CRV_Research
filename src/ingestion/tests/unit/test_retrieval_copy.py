from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ingest_pipeline.adapters.memory.clock import ManualClock
from ingest_pipeline.adapters.memory.evidence import InMemoryEvidenceStore
from ingest_pipeline.adapters.postgres import retrieval
from ingest_pipeline.adapters.postgres.retrieval import (
    PendingContent,
    PostgresRetrievalProjector,
    build_vector_index,
)
from ingest_pipeline.domain.errors import InvalidInputError
from ingest_pipeline.domain.planning import JobPlanner
from ingest_pipeline.domain.projection import ProjectionState
from ingest_pipeline.runtime import retrieval_copy
from ingest_pipeline.runtime.retrieval_copy import CopyOptions, copy_release
from ingest_pipeline.stages.base import StageServices
from ingest_pipeline.stages.project_search import ProjectionSettings

START = datetime(2026, 10, 23, tzinfo=UTC)
HASH = "c" * 64


def _row(key: str, digest: str = "a" * 64, **changes: Any) -> dict[str, Any]:
    return {
        "projection_id": key, "release_id": "crv-v1", "chunk_id": f"chunk:{key}",
        "content_hash": HASH, "chunk_kind": "table", "title": "Rapport.pdf - p. 3",
        "content": "Récupération Cu 92 %", "source_uri": "raw/Rapport.pdf",
        "source_path": "Dossier Cu/Rapport.pdf", "source_paths": ("Dossier Cu/Rapport.pdf",),
        "document_code": "RA-2019-05", "identifiers": ("RA-2019-05",),
        "original_page_numbers": (3,), "source_locators": (), "original_source_uris": None,
        "original_content_hashes": [HASH], "is_generated": False, "document_class": "report",
        "citation_basis": "source", "extraction_route": "di_layout", "language": "fr",
        "payload_sha256": digest, "content_vector": [0.25] * 3072, "placement_id": "plc:1",
        "evidence_ids": ["ev:1"], **changes,
    }


class _Connection:
    def __init__(self, table: dict[str, dict[str, Any]]) -> None:
        self.table = table
        self.statements: list[str] = []
        self.counts = {"rows": 2, "vectors": 2}

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append(query)
        if query.lstrip().startswith("INSERT INTO crv_retrieval.chunk"):
            records, now = args
            assert now == START
            for record in records:
                self.table[record["projection_id"]] = record
        elif query.startswith("DELETE FROM crv_retrieval.chunk"):
            for key in args[0]:
                self.table.pop(key, None)
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM crv_retrieval.chunk" in query
        return [
            {"projection_id": key, "payload_sha256": row["payload_sha256"],
             "has_vector": row["content_vector"] is not None}
            for key, row in self.table.items() if key in args[0]
        ]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, int]:
        return self.counts

    async def fetchval(self, query: str, *args: Any) -> str:
        return "12 MB"


class _Pool:
    def __init__(self) -> None:
        self.table: dict[str, dict[str, Any]] = {}
        self.connection = _Connection(self.table)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Connection]:
        yield self.connection


def _projector(pool: _Pool) -> PostgresRetrievalProjector:
    return PostgresRetrievalProjector(pool, ManualClock(START))


async def test_upload_writes_search_fields_only_and_rejects_bad_rows() -> None:
    pool = _Pool()
    report = await _projector(pool).upload(
        [_row("k1"), _row("k2", content_vector=[0.1] * 10), _row("k3", title="")],
        key_field="projection_id",
    )

    assert report.accepted == ("k1",)
    assert set(report.permanent) == {"k2", "k3"}
    stored = pool.table["k1"]
    assert set(stored) == {name for name, _ in retrieval._COLUMNS}
    assert stored["identifiers"] == ["RA-2019-05"]
    assert stored["original_source_uris"] == []
    assert stored["source_locators"] == []


async def test_upload_refuses_duplicates_and_other_keys() -> None:
    with pytest.raises(InvalidInputError):
        await _projector(_Pool()).upload([_row("k1"), _row("k1")], key_field="projection_id")
    with pytest.raises(InvalidInputError):
        await _projector(_Pool()).upload([_row("k1")], key_field="id")


async def test_verify_reports_missing_mismatched_and_vectorless_rows() -> None:
    pool = _Pool()
    projector = _projector(pool)
    await projector.upload([_row("k1"), _row("k2")], key_field="projection_id")
    pool.table["k2"]["content_vector"] = None

    problems = await projector.verify(
        {"k1": "a" * 64, "k2": "a" * 64, "k3": "a" * 64, "k4": "a" * 64},
        key_field="projection_id", digest_field="payload_sha256",
    )
    assert problems == {"k2": "missing_vector", "k3": "missing", "k4": "missing"}
    pool.table["k1"]["payload_sha256"] = "b" * 64
    assert await projector.verify(
        {"k1": "a" * 64}, key_field="projection_id", digest_field="payload_sha256"
    ) == {"k1": "digest_mismatch"}

    deleted = await projector.delete(["k1", "k9"], key_field="projection_id")
    assert deleted.accepted == ("k1", "k9")
    assert "k1" not in pool.table


async def test_vector_index_uses_half_precision_for_3072_dimensions() -> None:
    connection = _Pool().connection
    report = await build_vector_index(
        connection, "te3", m=16, ef_construction=128, maintenance_work_mem="2GB",
        parallel_workers=2,
    )

    [create] = [s for s in connection.statements if s.startswith("CREATE INDEX")]
    assert "(embedding_te3::halfvec(3072)) halfvec_cosine_ops" in create
    assert "m = 16, ef_construction = 128" in create
    assert "ANALYZE crv_retrieval.chunk" in connection.statements
    assert report == {"index": "chunk_te3_hnsw_idx", "model": "te3", "m": 16,
                      "ef_construction": 128, "rows": 2, "vectors": 2, "size": "12 MB"}


@pytest.mark.parametrize(
    ("model", "memory", "m"),
    [("other", "2GB", 16), ("te3", "2GB; DROP TABLE x", 16), ("te3", "2GB", 1)],
)
async def test_vector_index_refuses_unsafe_parameters(model: str, memory: str, m: int) -> None:
    with pytest.raises(InvalidInputError):
        await build_vector_index(
            _Pool().connection, model, m=m, ef_construction=128,
            maintenance_work_mem=memory, parallel_workers=2,
        )


class _Evidence:
    async def get_document(self, evidence_id: str) -> Any:
        return SimpleNamespace(evidence_id=evidence_id)


class _Snapshots:
    def __init__(self, placements: list[Any]) -> None:
        self.placements = placements

    async def for_content(self, content_hash: str) -> list[Any]:
        return self.placements


def _services(store: InMemoryEvidenceStore, placements: list[Any] | None = None) -> StageServices:
    placement = SimpleNamespace(blob_name="crv/Rapport.pdf", placement_id="plc:1")
    return StageServices(
        clock=ManualClock(START), planner=JobPlanner(versions={}),
        evidence=_Evidence(),
        snapshots=_Snapshots([placement] if placements is None else placements),
        projections=store,
    )


def _copy(
    monkeypatch: pytest.MonkeyPatch, pending: list[PendingContent], rows: dict[str, Any] | Exception
) -> None:
    async def fake_pending(pool: Any, **_: Any) -> list[PendingContent]:
        return pending

    async def fake_rows(*_: Any, **__: Any) -> dict[str, Any]:
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(retrieval_copy, "pending_contents", fake_pending)
    monkeypatch.setattr(retrieval_copy, "projection_rows", fake_rows)


SETTINGS = ProjectionSettings(target="crv-evidence-v1", corpus="crv", release_id="crv-v1")


def _options(**changes: Any) -> CopyOptions:
    values: dict[str, Any] = {
        "source_store": "azure_search", "source_target": "crv-evidence-v1",
        "model": "text-embedding-3-large", "dimensions": 3072, "limit": 10, **changes,
    }
    return CopyOptions(**values)


async def test_copy_writes_identical_rows_and_records_them_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [PendingContent("evd:1", HASH, {"k1": "a" * 64, "k2": "a" * 64})]
    _copy(monkeypatch, pending, {"k1": _row("k1"), "k2": _row("k2")})
    store, pool = InMemoryEvidenceStore(), _Pool()

    report = await copy_release(_services(store), pool, SETTINGS, _options())

    assert (report.contents, report.rows, report.identical_rows, report.copied_rows) == (1, 2, 2, 2)
    assert set(pool.table) == {"k1", "k2"}
    items = await store.items("postgres", "crv_retrieval", ["k1", "k2"])
    assert {item.state for item in items} == {ProjectionState.VERIFIED}
    assert {item.evidence_id for item in items} == {"evd:1"}


async def test_copy_skips_drift_unless_allowed_and_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [PendingContent("evd:1", HASH, {"k1": "a" * 64, "gone": "a" * 64})]
    _copy(monkeypatch, pending, {"k1": _row("k1", digest="b" * 64), "new": _row("new")})
    pool = _Pool()

    skipped = await copy_release(_services(InMemoryEvidenceStore()), pool, SETTINGS, _options())
    assert (skipped.drifted_contents, skipped.skipped_contents, skipped.copied_rows) == (1, 1, 0)
    assert (skipped.missing_rows, skipped.extra_rows, skipped.digest_mismatches) == (1, 1, 1)
    assert skipped.problems[0]["problem"] == "drift"

    dry = await copy_release(
        _services(InMemoryEvidenceStore()), pool, SETTINGS,
        _options(allow_drift=True, dry_run=True),
    )
    assert (dry.drifted_contents, dry.copied_rows, pool.table) == (1, 0, {})

    allowed = await copy_release(
        _services(InMemoryEvidenceStore()), pool, SETTINGS, _options(allow_drift=True)
    )
    assert allowed.copied_rows == 2
    assert set(pool.table) == {"k1", "new"}


async def test_copy_records_failures_and_unplaced_contents_per_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [PendingContent("evd:1", HASH, {"k1": "a" * 64})]
    _copy(monkeypatch, pending, RuntimeError("blob unavailable"))

    failed = await copy_release(
        _services(InMemoryEvidenceStore()), _Pool(), SETTINGS, _options()
    )
    assert failed.failed_contents == 1
    assert "blob unavailable" in failed.problems[0]["error"]

    unplaced = await copy_release(
        _services(InMemoryEvidenceStore(), placements=[]), _Pool(), SETTINGS, _options()
    )
    assert (unplaced.skipped_contents, unplaced.problems[0]["problem"]) == (
        1, "no_current_placement",
    )
