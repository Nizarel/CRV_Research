"""Evidence, embedding-cache and projection repository contract (memory and PostgreSQL)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from pipeline_harness import START

from tizertmaint_pipeline.adapters.memory.evidence import InMemoryEvidenceStore
from tizertmaint_pipeline.domain.errors import IntegrityError
from tizertmaint_pipeline.domain.models import ArtifactRef
from tizertmaint_pipeline.domain.projection import ProjectionItem, ProjectionState
from tizertmaint_pipeline.ports.evidence import (
    CachedExtraction,
    ChunkRecord,
    EvidenceRecord,
    EvidenceStatus,
    FigureAsset,
    FigureState,
)

pytestmark = pytest.mark.contract

CONTENT = "e" * 64
PLAN = "plan:contract"
UNIT = "unit:contract:00000"


def _ref(digit: str) -> ArtifactRef:
    return ArtifactRef(
        sha256=digit * 64, uri=f"artifacts/{digit}", size_bytes=1, media_type="application/json"
    )


@dataclass
class Backend:
    store: Any
    pool: Any = None


async def _seed(pool: Any) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO ingest.plan (plan_id, content_hash, policy_version, family, unit_count,"
            " created_at) VALUES ($1, $2, 'routing-v1', 'pdf', 1, $3)",
            PLAN,
            CONTENT,
            START,
        )
        await connection.execute(
            "INSERT INTO ingest.unit (unit_id, plan_id, ordinal, kind, route, created_at)"
            " VALUES ($1, $2, 0, 'pdf_pages', 'di_layout', $3)",
            UNIT,
            PLAN,
            START,
        )


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def backend(request: pytest.FixtureRequest) -> Backend:
    if request.param == "memory":
        return Backend(InMemoryEvidenceStore())
    from tizertmaint_pipeline.adapters.postgres.evidence import PostgresEvidenceStore

    pool = request.getfixturevalue("pg_pool")
    return Backend(PostgresEvidenceStore(pool), pool)


@pytest.fixture
async def store(backend: Backend) -> Any:
    if backend.pool is not None:
        await _seed(backend.pool)
    return backend.store


def _record(evidence_id: str, *, chunk_set: str = "1", minutes: int = 0) -> EvidenceRecord:
    moment = START + timedelta(minutes=minutes)
    return EvidenceRecord(
        evidence_id=evidence_id,
        plan_id=PLAN,
        content_hash=CONTENT,
        policy_version="routing-v1",
        completeness="complete",
        status=EvidenceStatus.CHUNKED,
        manifest=_ref("a"),
        chunk_set=_ref(chunk_set),
        chunk_count=1,
        created_at=moment,
        updated_at=moment,
    )


def _chunk(evidence_id: str) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"chunk:{evidence_id}",
        evidence_id=evidence_id,
        ordinal=0,
        kind="prose",
        chunker="evidence-chunker@1",
        tokenizer="approx:utf8/3.2",
        text_sha256="b" * 64,
        token_count=12,
        pages=(1, 2),
    )


async def test_documents_are_idempotent_and_conflicts_rejected(store: Any) -> None:
    first = await store.record_document(_record("evd:1"), [_chunk("evd:1")])
    again = await store.record_document(_record("evd:1", minutes=5), [_chunk("evd:1")])
    assert again == first
    with pytest.raises(IntegrityError):
        await store.record_document(_record("evd:1", chunk_set="2"), [])
    assert await store.get_document("evd:missing") is None


async def test_latest_embedded_document(store: Any) -> None:
    await store.record_document(_record("evd:old"), [_chunk("evd:old")])
    await store.record_document(_record("evd:new", minutes=1), [_chunk("evd:new")])
    latest = await store.latest_document(CONTENT, "routing-v1", status=EvidenceStatus.EMBEDDED)
    assert latest is None
    await store.mark_embedded("evd:old", now=START + timedelta(minutes=2))
    await store.mark_embedded("evd:old", now=START + timedelta(minutes=9))
    latest = await store.latest_document(CONTENT, "routing-v1", status=EvidenceStatus.EMBEDDED)
    assert latest is not None
    assert latest.evidence_id == "evd:old"
    assert latest.updated_at == START + timedelta(minutes=2)


async def test_figures_never_downgrade_once_stored(store: Any) -> None:
    await store.record_figures(
        [FigureAsset(unit_id=UNIT, figure_id="1.1", state=FigureState.FAILED, reason="x")],
        now=START,
    )
    stored = FigureAsset(
        unit_id=UNIT, figure_id="1.1", state=FigureState.STORED, artifact=_ref("f")
    )
    await store.record_figures([stored], now=START)
    await store.record_figures(
        [FigureAsset(unit_id=UNIT, figure_id="1.1", state=FigureState.EXPIRED, reason="late")],
        now=START,
    )
    assert await store.figures([UNIT]) == [stored]


async def test_embedding_cache_first_writer_wins(store: Any) -> None:
    key = "c" * 64
    await store.put_many({key: (0.5, -1.0, 2.25)}, model="m", dimensions=3, now=START)
    await store.put_many({key: (9.0, 9.0, 9.0)}, model="m", dimensions=3, now=START)
    assert await store.get_many([key, "d" * 64], model="m", dimensions=3) == {
        key: (0.5, -1.0, 2.25)
    }
    assert await store.get_many([key], model="other", dimensions=3) == {}
    with pytest.raises(IntegrityError):
        await store.put_many({"d" * 64: (1.0,)}, model="m", dimensions=3, now=START)


async def test_extraction_cache_first_writer_wins(store: Any) -> None:
    first = CachedExtraction(
        fingerprint="f" * 64,
        provider="content_understanding",
        content_hash=CONTENT,
        raw=_ref("a"),
        page_map={"1": 3},
        details={"pages": 1},
    )
    await store.cache_extraction(first, now=START)
    await store.cache_extraction(first.model_copy(update={"raw": _ref("b")}), now=START)
    assert await store.cached_extraction("f" * 64) == first
    assert await store.cached_extraction("0" * 64) is None


def _item(
    key: str,
    *,
    digest: str = "1",
    placement: str = "pl-1",
    evidence: str = "evd:1",
    content: str = CONTENT,
) -> ProjectionItem:
    return ProjectionItem(
        store="azure_search",
        target="index",
        projection_id=key,
        release_id="shadow",
        evidence_id=evidence,
        chunk_id=f"chunk:{key}",
        content_hash=content,
        placement_id=placement,
        payload_sha256=digest * 64,
    )


async def test_projection_items_track_expected_state(store: Any) -> None:
    await store.upsert([_item("k1"), _item("k2"), _item("k3", content="f" * 64)], now=START)
    await store.set_state(
        "azure_search",
        "index",
        {
            "k1": (ProjectionState.ACKNOWLEDGED, None),
            "k2": (ProjectionState.FAILED_PERMANENT, "HTTP 400"),
        },
        now=START,
        count_attempt=True,
    )
    await store.upsert([_item("k1")], now=START)
    items = {i.projection_id: i for i in await store.items("azure_search", "index", ["k1", "k2"])}
    assert items["k1"].state is ProjectionState.ACKNOWLEDGED
    assert items["k1"].attempts == 1
    assert items["k2"].last_error == "HTTP 400"

    await store.upsert([_item("k1", digest="2"), _item("k2")], now=START)
    items = {i.projection_id: i for i in await store.items("azure_search", "index", ["k1", "k2"])}
    assert items["k1"].state is ProjectionState.PENDING
    assert items["k1"].payload_sha256 == "2" * 64
    assert items["k2"].state is ProjectionState.PENDING

    await store.set_state(
        "azure_search",
        "index",
        {"k1": (ProjectionState.VERIFIED, None), "k2": (ProjectionState.RETIRED, None)},
        now=START,
    )
    active = await store.active_for_content("azure_search", "index", CONTENT)
    assert sorted(item.projection_id for item in active) == ["k1"]
    await store.upsert([_item("k4")], now=START)
    active = await store.active_for_content("azure_search", "index", CONTENT)
    assert sorted(item.projection_id for item in active) == ["k1", "k4"]

    await store.upsert([_item("k1", digest="3", placement="pl-9")], now=START)
    [moved] = await store.items("azure_search", "index", ["k1"])
    assert (moved.placement_id, moved.state) == ("pl-9", ProjectionState.PENDING)
