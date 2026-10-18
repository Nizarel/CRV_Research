"""End-to-end recovery scenarios over the in-memory adapters (ING-A01/A03/A04/A11/A16)."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from pipeline_harness import LEASE_SECONDS, FunctionStage, Harness, HarnessFactory

from tizertmaint_pipeline.domain.envelope import WorkEnvelope
from tizertmaint_pipeline.domain.errors import (
    ErrorCategory,
    InvalidInputError,
    TransientError,
    UnsupportedContentError,
)
from tizertmaint_pipeline.domain.models import ArtifactLink, StageJob, StageOutcome
from tizertmaint_pipeline.domain.states import JobStatus, StageName, WorkerPool
from tizertmaint_pipeline.faults import FaultPoint, SimulatedCrash
from tizertmaint_pipeline.runtime.executor import Disposition
from tizertmaint_pipeline.stages.base import StageContext

BLOB = "2-Zone Broyage/1-Grinding 1/6220-PP-2211 Pump/Datasheets/pump.pdf"
DATA = b"%PDF-1.7 pump datasheet"


async def _status(harness: Harness, job_id: str) -> JobStatus:
    job = await harness.ledger.get(job_id)
    assert job is not None
    return job.status


async def _crash_once(harness: Harness, pool: WorkerPool = WorkerPool.IO) -> None:
    await harness.dispatcher().dispatch_once()
    with pytest.raises(SimulatedCrash):
        await harness.worker(pool).run(exit_when_idle=True)


async def _recover_after_lease(harness: Harness) -> None:
    harness.clock.advance(LEASE_SECONDS + 1)
    harness.bus.expire_locks()
    await harness.reconciler().reconcile_once()


async def test_snapshot_happy_path(harness: Harness) -> None:
    spec = await harness.plan_snapshot(BLOB, DATA)

    await harness.drain()

    assert await _status(harness, spec.job_id) is JobStatus.SUCCEEDED
    assert len(harness.snapshots.all()) == 1
    assert harness.snapshot_handler.calls == 1
    assert harness.bus.locked[WorkerPool.IO] == []
    assert harness.telemetry.stages[0]["status"] == "succeeded"


async def test_a01_crash_after_registration_resumes_without_deleting_records(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(faults={FaultPoint.AFTER_REGISTER: 1})
    spec = await harness.plan_snapshot(BLOB, DATA)

    await _crash_once(harness)
    [registered] = harness.snapshots.all()
    assert await _status(harness, spec.job_id) is JobStatus.LEASED

    await _recover_after_lease(harness)
    await harness.drain()

    assert await _status(harness, spec.job_id) is JobStatus.SUCCEEDED
    assert harness.snapshots.all() == [registered]
    attempts = await harness.ledger.attempts(spec.job_id)
    assert [(a.outcome, a.error_category) for a in attempts] == [
        (JobStatus.RETRY_WAIT, ErrorCategory.LEASE_EXPIRED),
        (JobStatus.SUCCEEDED, None),
    ]


async def test_a04_crash_after_commit_before_settle_does_not_repeat_work(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(faults={FaultPoint.AFTER_COMMIT_BEFORE_SETTLE: 1})
    spec = await harness.plan_snapshot(BLOB, DATA)

    await _crash_once(harness)
    assert await _status(harness, spec.job_id) is JobStatus.SUCCEEDED
    harness.bus.expire_locks()
    await harness.drain()

    assert harness.snapshot_handler.calls == 1
    assert harness.bus.dead_letters[WorkerPool.IO] == []


async def test_a04_crash_after_artifact_write_reuses_the_content_addressed_object(
    make_harness: HarnessFactory,
) -> None:
    async def write_raw(context: StageContext, job: StageJob) -> StageOutcome:
        ref = await context.services.require_artifacts().put(
            b'{"raw":"provider result"}', media_type="application/json"
        )
        context.services.faults.hit(FaultPoint.AFTER_ARTIFACT_WRITE)
        return StageOutcome.succeeded(artifacts=(ArtifactLink(role="raw", artifact=ref),))

    stage = FunctionStage(write_raw)
    harness = make_harness(
        faults={FaultPoint.AFTER_ARTIFACT_WRITE: 1}, stages={StageName.NORMALIZE: stage}
    )
    spec = harness.services.planner.spec(StageName.NORMALIZE, unit_id="unit-1")
    await harness.ledger.enqueue([spec], now=harness.clock.now())

    await _crash_once(harness, WorkerPool.CPU)
    await _recover_after_lease(harness)
    await harness.drain()

    assert stage.calls == 2
    assert harness.artifacts.put_calls == 2
    assert len(harness.artifacts.objects) == 1
    digest = hashlib.sha256(b'{"raw":"provider result"}').hexdigest()
    assert harness.ledger.job_artifacts(spec.job_id) == {("raw", digest)}


async def test_a04_duplicate_sends_after_dispatcher_crash_execute_once(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(faults={FaultPoint.AFTER_SEND_BEFORE_MARK: 1})
    await harness.plan_snapshot(BLOB, DATA)

    with pytest.raises(SimulatedCrash):
        await harness.dispatcher().dispatch_once()
    harness.clock.advance(31)
    await harness.drain()

    assert len(harness.bus.sent) == 2
    assert harness.snapshot_handler.calls == 1


async def test_a03_concurrent_duplicate_deliveries_execute_once(
    make_harness: HarnessFactory,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow(context: StageContext, job: StageJob) -> StageOutcome:
        started.set()
        await gate.wait()
        return StageOutcome.succeeded()

    stage = FunctionStage(slow)
    harness = make_harness(stages={StageName.NORMALIZE: stage})
    spec = harness.services.planner.spec(StageName.NORMALIZE, unit_id="unit-1")
    await harness.ledger.enqueue([spec], now=harness.clock.now())
    [event] = await harness.ledger.lease_batch(now=harness.clock.now(), limit=1, claim_seconds=30)

    first = asyncio.create_task(harness.executor.process(event.envelope))
    await started.wait()
    duplicate = await harness.executor.process(event.envelope)
    gate.set()
    winner = await first

    assert (duplicate.disposition, duplicate.reason) == (
        Disposition.COMPLETE,
        "not_claimable:leased",
    )
    assert winner.executed
    assert not duplicate.executed
    assert stage.calls == 1


async def test_a11_changed_source_supersedes_and_processes_current_bytes(harness: Harness) -> None:
    old_etag = harness.put_source(BLOB, DATA)
    stale = harness.snapshot_spec(
        BLOB, etag=old_etag, content_hash=hashlib.sha256(DATA).hexdigest()
    )
    await harness.ledger.enqueue([stale], now=harness.clock.now())
    revised = b"%PDF-1.7 pump datasheet rev B"
    harness.put_source(BLOB, revised)

    await harness.drain()

    assert await _status(harness, stale.job_id) is JobStatus.SUPERSEDED
    [snapshot] = harness.snapshots.all()
    assert snapshot.content_hash == hashlib.sha256(revised).hexdigest()
    successors = [job for job in harness.ledger.jobs() if job.parent_job_id == stale.job_id]
    assert [job.status for job in successors] == [JobStatus.SUCCEEDED]


async def test_a16_malformed_and_unknown_messages_are_dead_lettered_once(harness: Harness) -> None:
    harness.bus.inject(WorkerPool.IO, b"{not json")
    unknown = WorkEnvelope(
        event_id="e-1",
        job_id="missing",
        stage=StageName.SNAPSHOT,
        unit_id="u",
        fingerprint="a" * 64,
    )
    harness.bus.inject(WorkerPool.IO, unknown.to_bytes())

    await harness.worker().run(exit_when_idle=True)

    dead = harness.bus.dead_letters[WorkerPool.IO]
    assert [reason.split(":")[0] for _, reason in dead] == ["invalid_envelope", "unknown_job"]
    assert [message.delivery_count for message, _ in dead] == [1, 1]


async def test_a16_permanent_failures_are_classified_once(make_harness: HarnessFactory) -> None:
    async def corrupt(context: StageContext, job: StageJob) -> StageOutcome:
        raise InvalidInputError("corrupt structure")

    async def encrypted(context: StageContext, job: StageJob) -> StageOutcome:
        raise UnsupportedContentError("encrypted PDF")

    invalid, unsupported = FunctionStage(corrupt), FunctionStage(encrypted)
    harness = make_harness(stages={StageName.NORMALIZE: invalid, StageName.CHUNK: unsupported})
    planner = harness.services.planner
    specs = [
        planner.spec(StageName.NORMALIZE, unit_id="u1"),
        planner.spec(StageName.CHUNK, unit_id="u2"),
    ]
    await harness.ledger.enqueue(specs, now=harness.clock.now())

    await harness.drain()

    assert [await _status(harness, spec.job_id) for spec in specs] == [
        JobStatus.QUARANTINED,
        JobStatus.UNSUPPORTED,
    ]
    assert (invalid.calls, unsupported.calls) == (1, 1)
    assert all(not dead for dead in harness.bus.dead_letters.values())


async def test_a16_transient_failures_spend_one_retry_budget(make_harness: HarnessFactory) -> None:
    async def flaky(context: StageContext, job: StageJob) -> StageOutcome:
        raise TransientError("connection reset")

    stage = FunctionStage(flaky)
    harness = make_harness(stages={StageName.NORMALIZE: stage}, max_failures=3)
    spec = harness.services.planner.spec(StageName.NORMALIZE, unit_id="u1")
    await harness.ledger.enqueue([spec], now=harness.clock.now())

    await harness.drain()

    job = await harness.ledger.get(spec.job_id)
    assert job is not None
    assert (job.status, job.failure_count, stage.calls) == (JobStatus.FAILED_PERMANENT, 3, 3)
    assert all(not dead for dead in harness.bus.dead_letters.values())
    assert harness.worker(WorkerPool.CPU).summary.completed == 3


async def test_successor_jobs_flow_to_their_pool(make_harness: HarnessFactory) -> None:
    async def accept(context: StageContext, job: StageJob) -> StageOutcome:
        return StageOutcome.succeeded()

    preflight = FunctionStage(accept)

    def successors(snapshot, planner):
        return (
            planner.spec(
                StageName.PREFLIGHT,
                unit_id=snapshot.snapshot_id,
                inputs={"snapshot_id": snapshot.snapshot_id},
                source_snapshot_id=snapshot.snapshot_id,
                content_hash=snapshot.content_hash,
            ),
        )

    harness = make_harness(successors=successors, stages={StageName.PREFLIGHT: preflight})
    spec = await harness.plan_snapshot(BLOB, DATA)

    await harness.drain()

    assert preflight.calls == 1
    [child] = [job for job in harness.ledger.jobs() if job.stage is StageName.PREFLIGHT]
    assert (child.parent_job_id, child.status) == (spec.job_id, JobStatus.SUCCEEDED)
    sent = [env for env in harness.bus.sent if env.stage is StageName.PREFLIGHT]
    assert sent[0].source_snapshot_id == harness.snapshots.all()[0].snapshot_id


async def test_stage_timeouts_are_classified_and_retried(make_harness: HarnessFactory) -> None:
    async def hang(context: StageContext, job: StageJob) -> StageOutcome:
        await asyncio.sleep(10)
        return StageOutcome.succeeded()

    harness = make_harness(
        stages={StageName.NORMALIZE: FunctionStage(hang)},
        stage_timeout_seconds=0.05,
        max_failures=1,
    )
    spec = harness.services.planner.spec(StageName.NORMALIZE, unit_id="u1")
    await harness.ledger.enqueue([spec], now=harness.clock.now())

    await harness.drain()

    job = await harness.ledger.get(spec.job_id)
    assert job is not None
    assert (job.status, job.last_error_category) == (
        JobStatus.FAILED_PERMANENT,
        ErrorCategory.TIMEOUT,
    )


async def _start_blocked(harness: Harness, gate: asyncio.Event) -> asyncio.Task:
    spec = harness.services.planner.spec(StageName.NORMALIZE, unit_id="u1")
    await harness.ledger.enqueue([spec], now=harness.clock.now())
    [event] = await harness.ledger.lease_batch(now=harness.clock.now(), limit=1, claim_seconds=30)
    return asyncio.create_task(harness.executor.process(event.envelope))


async def test_heartbeats_keep_long_work_leased(make_harness: HarnessFactory) -> None:
    gate = asyncio.Event()

    async def wait(context: StageContext, job: StageJob) -> StageOutcome:
        await gate.wait()
        return StageOutcome.succeeded()

    harness = make_harness(
        stages={StageName.NORMALIZE: FunctionStage(wait)}, heartbeat_seconds=0.01
    )
    task = await _start_blocked(harness, gate)
    await asyncio.sleep(0.02)
    harness.clock.advance(50)
    await asyncio.sleep(0.05)
    harness.clock.advance(20)

    assert (await harness.reconciler().reconcile_once()).requeued == 0
    gate.set()
    result = await asyncio.wait_for(task, 1)
    assert result.executed
    assert result.reason is None


async def test_lost_lease_cancels_work_and_never_commits(make_harness: HarnessFactory) -> None:
    gate = asyncio.Event()

    async def wait(context: StageContext, job: StageJob) -> StageOutcome:
        await gate.wait()
        return StageOutcome.succeeded()

    harness = make_harness(
        stages={StageName.NORMALIZE: FunctionStage(wait)}, heartbeat_seconds=0.01
    )
    task = await _start_blocked(harness, gate)
    await asyncio.sleep(0.02)
    harness.clock.advance(LEASE_SECONDS + 1)
    await harness.reconciler().reconcile_once()

    result = await asyncio.wait_for(task, 1)

    assert result.reason == "lease_lost"
    [job] = harness.ledger.jobs()
    assert job.status is JobStatus.RETRY_WAIT
    assert any(name == "pipeline.lease_lost" for name, _, _ in harness.telemetry.counters)


async def test_worker_processes_messages_concurrently(make_harness: HarnessFactory) -> None:
    both_started = asyncio.Barrier(2)

    async def rendezvous(context: StageContext, job: StageJob) -> StageOutcome:
        await asyncio.wait_for(both_started.wait(), 1)
        return StageOutcome.succeeded()

    harness = make_harness(stages={StageName.NORMALIZE: FunctionStage(rendezvous)})
    planner = harness.services.planner
    specs = [planner.spec(StageName.NORMALIZE, unit_id=f"u{index}") for index in range(2)]
    await harness.ledger.enqueue(specs, now=harness.clock.now())
    await harness.dispatcher().dispatch_once()

    summary = await harness.worker(WorkerPool.CPU, concurrency=2).run(exit_when_idle=True)

    assert summary.executed == 2
    assert [await _status(harness, spec.job_id) for spec in specs] == [JobStatus.SUCCEEDED] * 2


async def test_dispatcher_releases_events_when_the_broker_fails(harness: Harness) -> None:
    await harness.plan_snapshot(BLOB, DATA)
    harness.bus.fail_next_sends = 1
    dispatcher = harness.dispatcher()

    assert await dispatcher.dispatch_once() == 0
    assert harness.bus.depth(WorkerPool.IO) == 0
    harness.clock.advance(5)
    assert await dispatcher.dispatch_once() == 1
    assert any(name == "pipeline.outbox.send_failures" for name, _, _ in harness.telemetry.counters)
