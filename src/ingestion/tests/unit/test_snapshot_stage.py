from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from pipeline_harness import Harness, HarnessFactory

from ingest_pipeline.domain.errors import ConfigurationError, UnsupportedContentError
from ingest_pipeline.domain.ids import legacy_placement_id, legacy_source_object_id
from ingest_pipeline.domain.models import Lease, SourceProvenance
from ingest_pipeline.domain.states import JobStatus, StageName
from ingest_pipeline.stages.base import StageContext

BLOB = "2-Zone Broyage/1-Grinding 1/6220-PP-2211 Pump/Manuals/manual.pdf"
DATA = b"%PDF-1.7 pump manual"
DIGEST = hashlib.sha256(DATA).hexdigest()


async def _lease(harness: Harness, *, etag: str, content_hash: str | None = DIGEST) -> Lease:
    spec = harness.snapshot_spec(BLOB, etag=etag, content_hash=content_hash)
    await harness.ledger.enqueue([spec], now=harness.clock.now())
    lease = await harness.ledger.claim(
        spec.job_id, owner="test", lease_seconds=60, now=harness.clock.now()
    )
    assert lease is not None
    return lease


async def _run(harness: Harness, lease: Lease):
    handler = harness.registry.get(StageName.SNAPSHOT).handler
    return await handler.execute(StageContext(harness.services, lease), lease.job)


async def test_snapshot_records_exact_version_with_legacy_identifiers(harness: Harness) -> None:
    etag = harness.put_source(BLOB, DATA)
    outcome = await _run(harness, await _lease(harness, etag=etag))

    assert outcome.status is JobStatus.SUCCEEDED
    [snapshot] = harness.snapshots.all()
    source_id = legacy_source_object_id(harness.sources.container_url, BLOB)
    assert snapshot.source_object_id == source_id
    assert snapshot.placement_id == legacy_placement_id(source_id, BLOB)
    assert (snapshot.etag, snapshot.content_hash, snapshot.size_bytes) == (etag, DIGEST, len(DATA))
    assert outcome.checkpoint == {
        "snapshot_id": snapshot.snapshot_id,
        "content_hash": DIGEST,
        "declared_hash_matches": True,
    }


async def test_changed_etag_supersedes_and_plans_the_current_version(harness: Harness) -> None:
    old_etag = harness.put_source(BLOB, DATA)
    lease = await _lease(harness, etag=old_etag)
    new_etag = harness.put_source(BLOB, b"%PDF-1.7 revised")

    outcome = await _run(harness, lease)

    assert outcome.status is JobStatus.SUPERSEDED
    [successor] = outcome.next_jobs
    assert successor.inputs["expected_etag"] == new_etag
    assert "expected_content_hash" not in successor.inputs
    assert harness.snapshots.all() == []


async def test_bytes_differing_from_planned_hash_are_never_snapshotted(harness: Harness) -> None:
    etag = harness.put_source(BLOB, DATA)
    outcome = await _run(harness, await _lease(harness, etag=etag, content_hash="0" * 64))

    assert outcome.status is JobStatus.SUPERSEDED
    [successor] = outcome.next_jobs
    assert successor.inputs["expected_content_hash"] == DIGEST
    assert successor.content_hash == DIGEST
    assert harness.snapshots.all() == []


async def test_source_changing_or_disappearing_mid_read_supersedes(harness: Harness) -> None:
    etag = harness.put_source(BLOB, DATA)
    lease = await _lease(harness, etag=etag)

    original_stream = harness.sources.stream

    def racing_stream(blob_name: str, *, etag: str):
        harness.put_source(BLOB, b"replaced during read")
        return original_stream(blob_name, etag=etag)

    harness.sources.stream = racing_stream  # type: ignore[method-assign]
    outcome = await _run(harness, lease)
    assert outcome.status is JobStatus.SUPERSEDED
    assert outcome.next_jobs[0].inputs["expected_etag"] != etag

    def vanishing_stream(blob_name: str, *, etag: str):
        harness.sources._blobs.pop(BLOB)
        return original_stream(blob_name, etag=etag)

    harness.sources.stream = vanishing_stream  # type: ignore[method-assign]
    lease = await _lease(harness, etag=harness.put_source(BLOB, DATA))
    outcome = await _run(harness, lease)
    assert outcome.status is JobStatus.SUPERSEDED
    assert outcome.next_jobs == ()


async def test_snapshot_guards_size_and_container(make_harness: HarnessFactory) -> None:
    harness = make_harness(max_source_bytes=4)
    etag = harness.put_source(BLOB, DATA)
    with pytest.raises(UnsupportedContentError):
        await _run(harness, await _lease(harness, etag=etag))

    harness = make_harness()
    etag = harness.put_source(BLOB, DATA)
    lease = await _lease(harness, etag=etag)
    harness.sources.container_name = "other"
    with pytest.raises(ConfigurationError):
        await _run(harness, lease)


async def test_successor_factory_receives_the_stored_snapshot(make_harness: HarnessFactory) -> None:
    seen = []

    def successors(snapshot, planner):
        seen.append(snapshot.snapshot_id)
        return ()

    harness = make_harness(successors=successors)
    etag = harness.put_source(BLOB, DATA)
    await _run(harness, await _lease(harness, etag=etag))
    assert seen == [harness.snapshots.all()[0].snapshot_id]


async def test_snapshot_preserves_archive_and_conversion_provenance(harness: Harness, monkeypatch):
    etag = harness.put_source(BLOB, DATA)
    properties = await harness.sources.properties(BLOB)
    provenance = SourceProvenance(
        source_path="CMG/bundle.zip!/report.doc", original_hash="a" * 64,
        original_blob="sha256/original", archive_members=("report.doc",),
        converter="sha256:" + "b" * 64, manifest_blob="sha256/manifest",
    )

    async def with_provenance(_):
        return replace(properties, provenance=provenance)

    monkeypatch.setattr(harness.sources, "properties", with_provenance)
    outcome = await _run(harness, await _lease(harness, etag=etag))
    assert outcome.status is JobStatus.SUCCEEDED
    assert harness.snapshots.all()[0].provenance == provenance
