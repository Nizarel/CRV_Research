"""Behavioural contract shared by every ``JobLedger``/``Outbox``/``SnapshotRepository``.

The in-memory variant always runs; the PostgreSQL variant runs with ``-m integration``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from pipeline_harness import START

from tizertmaint_pipeline.adapters.memory.ledger import InMemoryLedger, InMemorySnapshots
from tizertmaint_pipeline.domain.errors import ErrorCategory, IntegrityError, LeaseLostError
from tizertmaint_pipeline.domain.models import (
    ArtifactLink,
    ArtifactRef,
    JobSpec,
    ProviderUsage,
    SourceSnapshot,
    StageOutcome,
)
from tizertmaint_pipeline.domain.states import JobStatus, StageName, WorkerPool

pytestmark = pytest.mark.contract


@dataclass
class Backend:
    ledger: Any
    snapshots: Any


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def backend(request: pytest.FixtureRequest) -> Backend:
    if request.param == "memory":
        return Backend(InMemoryLedger(), InMemorySnapshots())
    from tizertmaint_pipeline.adapters.postgres.ledger import PostgresLedger
    from tizertmaint_pipeline.adapters.postgres.snapshots import PostgresSnapshots

    pool = request.getfixturevalue("pg_pool")
    return Backend(PostgresLedger(pool), PostgresSnapshots(pool))


def _spec(unit: str = "unit-1", *, not_before: Any = None, max_failures: int = 3) -> JobSpec:
    return JobSpec.create(
        stage=StageName.SNAPSHOT,
        pool=WorkerPool.IO,
        unit_id=unit,
        stage_version="1",
        inputs={"unit": unit},
        max_failures=max_failures,
        not_before=not_before,
    )


def _at(seconds: float) -> Any:
    return START + timedelta(seconds=seconds)


async def _claim(backend: Backend, spec: JobSpec, *, at: float = 0, owner: str = "w1"):
    return await backend.ledger.claim(spec.job_id, owner=owner, lease_seconds=60, now=_at(at))


async def test_enqueue_is_idempotent_and_emits_one_wake_up(backend: Backend) -> None:
    spec = _spec()
    assert await backend.ledger.enqueue([spec, spec], now=START) == [spec.job_id]
    assert await backend.ledger.enqueue([spec], now=START) == []

    events = await backend.ledger.lease_batch(now=START, limit=10, claim_seconds=30)
    assert [event.job_id for event in events] == [spec.job_id]
    assert events[0].envelope.fingerprint == spec.fingerprint
    assert events[0].pool is WorkerPool.IO

    job = await backend.ledger.get(spec.job_id)
    assert (job.status, job.attempt, job.fencing_token) == (JobStatus.PENDING, 0, 0)
    assert job.inputs == {"unit": "unit-1"}


async def test_concurrent_claims_have_exactly_one_winner(backend: Backend) -> None:
    spec = _spec()
    await backend.ledger.enqueue([spec], now=START)

    leases = await asyncio.gather(
        *(_claim(backend, spec, owner=f"worker-{index}") for index in range(5))
    )

    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1
    job = await backend.ledger.get(spec.job_id)
    assert (job.status, job.attempt) == (JobStatus.LEASED, 1)
    [attempt] = await backend.ledger.attempts(spec.job_id)
    assert attempt.worker == winners[0].job.lease_owner


async def test_jobs_are_not_claimable_before_not_before(backend: Backend) -> None:
    spec = _spec(not_before=_at(60))
    await backend.ledger.enqueue([spec], now=START)
    assert await _claim(backend, spec, at=0) is None
    assert await _claim(backend, spec, at=61) is not None


async def test_stale_lease_cannot_commit_after_recovery(backend: Backend) -> None:
    spec = _spec()
    await backend.ledger.enqueue([spec], now=START)
    stale = await _claim(backend, spec)

    recovery = await backend.ledger.recover_expired(now=_at(61), limit=10)
    assert (recovery.requeued, recovery.failed) == (1, 0)
    job = await backend.ledger.get(spec.job_id)
    assert job.status is JobStatus.RETRY_WAIT
    assert (job.failure_count, job.last_error_category) == (1, ErrorCategory.LEASE_EXPIRED)

    with pytest.raises(LeaseLostError):
        await backend.ledger.heartbeat(stale, lease_seconds=60, now=_at(61))
    fresh = await _claim(backend, spec, at=61, owner="w2")
    assert fresh is not None
    assert fresh.fencing_token == 2
    with pytest.raises(LeaseLostError):
        await backend.ledger.complete(stale, StageOutcome.succeeded(), now=_at(62))
    await backend.ledger.complete(fresh, StageOutcome.succeeded(), now=_at(62))

    assert (await backend.ledger.get(spec.job_id)).status is JobStatus.SUCCEEDED
    attempts = await backend.ledger.attempts(spec.job_id)
    assert [(a.outcome, a.error_category) for a in attempts] == [
        (JobStatus.RETRY_WAIT, ErrorCategory.LEASE_EXPIRED),
        (JobStatus.SUCCEEDED, None),
    ]


async def test_heartbeat_extends_the_lease(backend: Backend) -> None:
    spec = _spec()
    await backend.ledger.enqueue([spec], now=START)
    lease = await _claim(backend, spec)

    renewed = await backend.ledger.heartbeat(lease, lease_seconds=60, now=_at(50))

    assert renewed.expires_at == _at(110)
    assert (await backend.ledger.recover_expired(now=_at(70), limit=10)).requeued == 0


async def test_repeated_lease_expiry_exhausts_the_failure_budget(backend: Backend) -> None:
    spec = _spec(max_failures=2)
    await backend.ledger.enqueue([spec], now=START)

    await _claim(backend, spec, at=0)
    assert (await backend.ledger.recover_expired(now=_at(61), limit=10)).requeued == 1
    await _claim(backend, spec, at=61)
    result = await backend.ledger.recover_expired(now=_at(122), limit=10)

    assert (result.requeued, result.failed) == (0, 1)
    assert (await backend.ledger.get(spec.job_id)).status is JobStatus.FAILED_PERMANENT


def _artifact(size: int = 3) -> ArtifactRef:
    digest = "a" * 64
    return ArtifactRef(
        sha256=digest, uri=f"sha256/aa/{digest}", size_bytes=size, media_type="application/json"
    )


async def test_completion_commits_outcome_artifacts_and_successors_together(
    backend: Backend,
) -> None:
    spec, successor = _spec("unit-1"), _spec("unit-2")
    await backend.ledger.enqueue([spec], now=START)
    await backend.ledger.lease_batch(now=START, limit=10, claim_seconds=30)
    lease = await _claim(backend, spec)

    await backend.ledger.complete(
        lease,
        StageOutcome.succeeded(
            artifacts=(ArtifactLink(role="raw", artifact=_artifact()),),
            next_jobs=(successor,),
            checkpoint={"snapshot_id": "s-1"},
        ),
        now=_at(5),
    )

    job = await backend.ledger.get(spec.job_id)
    assert (job.status, job.checkpoint, job.lease_owner) == (
        JobStatus.SUCCEEDED,
        {"snapshot_id": "s-1"},
        None,
    )
    child = await backend.ledger.get(successor.job_id)
    assert (child.status, child.parent_job_id) == (JobStatus.PENDING, spec.job_id)
    events = await backend.ledger.lease_batch(now=_at(5), limit=10, claim_seconds=30)
    assert [event.job_id for event in events] == [successor.job_id]


async def test_artifact_conflict_rolls_back_the_whole_completion(backend: Backend) -> None:
    first, second, successor = _spec("unit-1"), _spec("unit-2"), _spec("unit-3")
    await backend.ledger.enqueue([first, second], now=START)
    lease = await _claim(backend, first)
    await backend.ledger.complete(
        lease,
        StageOutcome.succeeded(artifacts=(ArtifactLink(role="raw", artifact=_artifact(3)),)),
        now=_at(1),
    )
    other = await _claim(backend, second)

    with pytest.raises(IntegrityError):
        await backend.ledger.complete(
            other,
            StageOutcome.succeeded(
                artifacts=(ArtifactLink(role="raw", artifact=_artifact(4)),),
                next_jobs=(successor,),
            ),
            now=_at(2),
        )

    assert (await backend.ledger.get(second.job_id)).status is JobStatus.LEASED
    assert await backend.ledger.get(successor.job_id) is None
    await backend.ledger.complete(other, StageOutcome.succeeded(), now=_at(3))


def _poll(unit: str, **inputs: Any) -> JobSpec:
    return JobSpec.create(
        stage=StageName.CU_POLL, pool=WorkerPool.IO, unit_id=unit, stage_version="1", inputs=inputs
    )


async def test_in_flight_work_and_spend_are_split_by_billing_account(backend: Backend) -> None:
    here = _poll("unit-1", billable_pages=2)
    partner = _poll("unit-2", billable_pages=3, account="p")
    done_here, done_partner = _poll("unit-3", billable_pages=1), _poll("unit-4", account="p")
    await backend.ledger.enqueue([here, partner, done_here, done_partner], now=START)
    for spec, account, cost in ((done_here, None, 0.25), (done_partner, "p", 1.5)):
        lease = await _claim(backend, spec)
        usage = ProviderUsage(
            provider="content_understanding", pages=1, account=account, cost_usd=cost
        )
        await backend.ledger.complete(lease, StageOutcome.succeeded(usage=usage), now=_at(1))

    work_here = await backend.ledger.account_work(StageName.CU_POLL, None)
    work_partner = await backend.ledger.account_work(StageName.CU_POLL, "p")
    assert (work_here.jobs, work_here.billable_pages) == (1, 2)
    assert (work_partner.jobs, work_partner.billable_pages) == (1, 3)
    assert (await backend.ledger.active_work(StageName.CU_POLL)).billable_pages == 5
    providers = ["document_intelligence", "content_understanding"]
    assert await backend.ledger.account_spend(None, providers) == pytest.approx(0.25)
    assert await backend.ledger.account_spend("p", providers) == pytest.approx(1.5)
    assert await backend.ledger.account_spend("p", ["document_intelligence"]) == 0


def _submit(unit: str) -> JobSpec:
    return JobSpec.create(
        stage=StageName.CU_SUBMIT, pool=WorkerPool.IO, unit_id=unit, stage_version="1"
    )


async def test_start_remote_counts_open_polls_and_admitted_submits_only(backend: Backend) -> None:
    submits = [_submit(f"unit-{index}") for index in range(3)]
    await backend.ledger.enqueue([_poll("unit-p"), *submits], now=START)
    first, second, third = [await _claim(backend, spec) for spec in submits]

    async def start(lease: Any, at: float = 1) -> bool:
        return await backend.ledger.start_remote(
            lease, polls=StageName.CU_POLL, limit=2, now=_at(at)
        )

    # Leased submits still waiting for admission must not hold a slot (rollout 2026-10-13).
    assert await start(first)
    assert not await start(second)
    assert await start(first)
    assert (await backend.ledger.get(submits[0].job_id)).status is JobStatus.RUNNING
    assert (await backend.ledger.active_work(StageName.CU_SUBMIT)).running == 1

    await backend.ledger.complete(first, StageOutcome.succeeded(), now=_at(2))
    assert await start(second, at=3)

    recovery = await backend.ledger.recover_expired(now=_at(61), limit=10)
    assert recovery.requeued == 2
    assert (await backend.ledger.active_work(StageName.CU_SUBMIT)).running == 0
    with pytest.raises(LeaseLostError):
        await start(third, at=62)


async def test_start_remote_counts_each_billing_account_separately(backend: Backend) -> None:
    submits = [_submit(f"unit-{index}") for index in range(3)]
    await backend.ledger.enqueue([_poll("unit-p", account="p"), *submits], now=START)
    first, second, third = [await _claim(backend, spec) for spec in submits]

    async def start(lease: Any, account: str | None) -> bool:
        return await backend.ledger.start_remote(
            lease, polls=StageName.CU_POLL, limit=1, account=account, now=_at(1)
        )

    assert await start(first, None)
    assert not await start(second, None)
    assert not await start(second, "p")
    assert await start(second, "q")
    assert not await start(third, "q")


async def test_concurrent_start_remote_admits_exactly_the_limit(backend: Backend) -> None:
    submits = [_submit(f"unit-{index}") for index in range(6)]
    await backend.ledger.enqueue(submits, now=START)
    leases = [await _claim(backend, spec) for spec in submits]

    admitted = await asyncio.gather(
        *(
            backend.ledger.start_remote(lease, polls=StageName.CU_POLL, limit=3, now=_at(1))
            for lease in leases
        )
    )

    assert sum(admitted) == 3
    assert (await backend.ledger.active_work(StageName.CU_SUBMIT)).running == 3


async def _park(backend: Backend, spec: JobSpec, *, at: float) -> None:
    lease = await _claim(backend, spec, at=at)
    await backend.ledger.complete(
        lease,
        StageOutcome(
            status=JobStatus.RETRY_WAIT,
            not_before=_at(1800),
            error_category=ErrorCategory.SLOT_WAIT,
            reason="CU in-flight limit reached",
        ),
        now=_at(at),
    )


async def test_a_finished_poll_wakes_the_oldest_parked_submit(backend: Backend) -> None:
    poll = _poll("unit-p")
    submits = [_submit(f"unit-{index}") for index in range(3)]
    await backend.ledger.enqueue([poll], now=START)
    for index, spec in enumerate(submits):
        await backend.ledger.enqueue([spec], now=_at(index))
        await _park(backend, spec, at=5)
    await backend.ledger.lease_batch(now=_at(9), limit=50, claim_seconds=30)

    lease = await _claim(backend, poll, at=10)
    await backend.ledger.complete(lease, StageOutcome.succeeded(), now=_at(10))

    jobs = [await backend.ledger.get(spec.job_id) for spec in submits]
    assert [job.not_before for job in jobs] == [_at(10), _at(1800), _at(1800)]
    events = await backend.ledger.lease_batch(now=_at(10), limit=50, claim_seconds=30)
    assert [event.job_id for event in events] == [submits[0].job_id]

    # One woken submit is still due but unadmitted, so only two of three slots are free.
    assert await backend.ledger.wake_parked(StageName.CU_SUBMIT, limit=3, now=_at(11)) == 2
    assert await backend.ledger.wake_parked(StageName.CU_SUBMIT, limit=3, now=_at(12)) == 0
    assert await backend.ledger.wake_parked(StageName.DI_SUBMIT, limit=3, now=_at(12)) == 0


async def test_parking_and_budget_waits_do_not_wake_other_submits(backend: Backend) -> None:
    first, second = _submit("unit-1"), _submit("unit-2")
    await backend.ledger.enqueue([first], now=START)
    await backend.ledger.enqueue([second], now=_at(1))
    await _park(backend, first, at=5)

    lease = await _claim(backend, second, at=6)
    await backend.ledger.complete(
        lease,
        StageOutcome(
            status=JobStatus.RETRY_WAIT,
            not_before=_at(300),
            error_category=ErrorCategory.THROTTLED,
            reason="CU page budget reached",
        ),
        now=_at(6),
    )

    assert (await backend.ledger.get(first.job_id)).not_before == _at(1800)


async def test_retry_wait_schedules_a_future_wake_up(backend: Backend) -> None:
    spec = _spec()
    await backend.ledger.enqueue([spec], now=START)
    [initial] = await backend.ledger.lease_batch(now=START, limit=10, claim_seconds=30)
    await backend.ledger.mark_sent([initial.event_id], now=START)
    lease = await _claim(backend, spec)

    await backend.ledger.complete(
        lease,
        StageOutcome(
            status=JobStatus.RETRY_WAIT,
            not_before=_at(30),
            counts_as_failure=True,
            error_category=ErrorCategory.TRANSIENT,
            reason="blip",
        ),
        now=_at(1),
    )

    assert await backend.ledger.lease_batch(now=_at(10), limit=10, claim_seconds=30) == []
    assert [
        e.job_id for e in await backend.ledger.lease_batch(now=_at(31), limit=10, claim_seconds=30)
    ] == [spec.job_id]
    job = await backend.ledger.get(spec.job_id)
    assert (job.failure_count, job.last_error) == (1, "blip")
    assert await _claim(backend, spec, at=10) is None
    assert await _claim(backend, spec, at=31) is not None


async def test_outbox_leases_release_and_mark_sent(backend: Backend) -> None:
    first, second = _spec("unit-1"), _spec("unit-2")
    await backend.ledger.enqueue([first, second], now=START)

    batch_one = await backend.ledger.lease_batch(now=START, limit=1, claim_seconds=30)
    batch_two = await backend.ledger.lease_batch(now=START, limit=1, claim_seconds=30)
    assert len(batch_one) == len(batch_two) == 1
    assert batch_one[0].event_id != batch_two[0].event_id
    assert await backend.ledger.lease_batch(now=START, limit=10, claim_seconds=30) == []

    await backend.ledger.release([batch_one[0].event_id], error="broker down", retry_at=_at(5))
    await backend.ledger.mark_sent([batch_two[0].event_id], now=_at(1))
    assert await backend.ledger.lease_batch(now=_at(2), limit=10, claim_seconds=30) == []
    again = await backend.ledger.lease_batch(now=_at(6), limit=10, claim_seconds=30)
    assert [event.event_id for event in again] == [batch_one[0].event_id]


async def test_redispatch_wakes_only_jobs_without_a_fresh_event(backend: Backend) -> None:
    spec = _spec()
    await backend.ledger.enqueue([spec], now=START)
    [event] = await backend.ledger.lease_batch(now=START, limit=10, claim_seconds=30)
    await backend.ledger.mark_sent([event.event_id], now=START)

    assert await backend.ledger.redispatch_stale(now=_at(10), stale_before=_at(-1), limit=10) == 0
    assert await backend.ledger.redispatch_stale(now=_at(10), stale_before=_at(5), limit=10) == 1
    assert await backend.ledger.redispatch_stale(now=_at(11), stale_before=_at(5), limit=10) == 0


def _snapshot(**overrides: Any) -> SourceSnapshot:
    fields: dict[str, Any] = {
        "snapshot_id": "urn:tizert:snapshot:1",
        "source_object_id": "source-1",
        "placement_id": "placement-1",
        "container_name": "content",
        "blob_name": "a.pdf",
        "etag": '"0x1"',
        "content_hash": "b" * 64,
        "size_bytes": 10,
        "observed_at": START,
    }
    fields.update(overrides)
    return SourceSnapshot.model_validate(fields)


async def test_snapshots_are_immutable_and_idempotent(backend: Backend) -> None:
    stored = await backend.snapshots.record(_snapshot())
    again = await backend.snapshots.record(_snapshot(observed_at=_at(60)))

    assert again == stored
    assert await backend.snapshots.get(stored.snapshot_id) == stored
    with pytest.raises(IntegrityError):
        await backend.snapshots.record(_snapshot(size_bytes=11))


async def test_content_placements_follow_each_placements_latest_bytes(backend: Backend) -> None:
    old, new = "b" * 64, "c" * 64
    await backend.snapshots.record(_snapshot())
    await backend.snapshots.record(
        _snapshot(
            snapshot_id="urn:tizert:snapshot:2",
            source_object_id="source-2",
            placement_id="placement-2",
            etag='"0x2"',
        )
    )
    await backend.snapshots.record(
        _snapshot(
            snapshot_id="urn:tizert:snapshot:3",
            etag='"0x3"',
            content_hash=new,
            observed_at=_at(5),
        )
    )

    assert [s.placement_id for s in await backend.snapshots.for_content(old)] == ["placement-2"]
    assert [s.placement_id for s in await backend.snapshots.for_content(new)] == ["placement-1"]
    assert await backend.snapshots.contents_for_placement("placement-1") == [old, new]
    assert await backend.snapshots.contents_for_placement("placement-2") == [old]
    assert await backend.snapshots.contents_for_placement("missing") == []
