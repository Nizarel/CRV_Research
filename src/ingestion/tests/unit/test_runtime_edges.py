from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pipeline_harness import Harness, build_harness

from tizertmaint_pipeline.adapters.memory.ledger import InMemoryLedger
from tizertmaint_pipeline.domain.errors import ConfigurationError, ErrorCategory
from tizertmaint_pipeline.domain.models import JobSpec
from tizertmaint_pipeline.domain.planning import JobPlanner
from tizertmaint_pipeline.domain.states import JobStatus, StageName, WorkerPool
from tizertmaint_pipeline.runtime.coordinator import Coordinator, OutboxDispatcher
from tizertmaint_pipeline.runtime.executor import StageExecutor
from tizertmaint_pipeline.runtime.registry import StageRegistry
from tizertmaint_pipeline.runtime.worker import Worker
from tizertmaint_pipeline.stages.base import StageServices

BLOB = "zone/a.pdf"


async def test_coordinator_dispatches_until_stopped_and_survives_step_errors(
    harness: Harness,
) -> None:
    await harness.plan_snapshot(BLOB, b"data")
    failures = {"left": 1}
    original = harness.ledger.lease_batch

    async def flaky_lease_batch(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database restarting")
        return await original(**kwargs)

    harness.ledger.lease_batch = flaky_lease_batch  # type: ignore[method-assign]
    stop = asyncio.Event()
    coordinator = Coordinator(
        dispatcher=harness.dispatcher(),
        reconciler=harness.reconciler(),
        dispatch_interval_seconds=0.01,
        reconcile_interval_seconds=0.01,
        dispatch_batch_size=100,
    )

    task = asyncio.create_task(coordinator.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 1)

    assert failures["left"] == 0
    assert harness.bus.depth(WorkerPool.IO) == 1


class _BrokenLedger(InMemoryLedger):
    async def get(self, job_id: str):
        raise ConnectionError("database unavailable")


async def test_worker_abandons_messages_when_infrastructure_fails() -> None:
    harness = build_harness()
    await harness.plan_snapshot(BLOB, b"data")
    await harness.dispatcher().dispatch_once()
    broken = StageExecutor(
        ledger=_BrokenLedger(),
        registry=harness.registry,
        services=harness.services,
        policy=harness.policy,
        settings=harness.executor._settings,
        telemetry=harness.telemetry,
    )
    worker = Worker(
        receiver=harness.bus.receiver(WorkerPool.IO),
        executor=broken,
        telemetry=harness.telemetry,
        max_wait_seconds=0,
    )

    summary = await worker.run(max_messages=1)

    assert (summary.abandoned, summary.completed) == (1, 0)
    assert harness.bus.depth(WorkerPool.IO) == 1


async def test_settlement_failures_are_counted_not_raised(harness: Harness) -> None:
    await harness.plan_snapshot(BLOB, b"data")
    await harness.dispatcher().dispatch_once()
    receiver = harness.bus.receiver(WorkerPool.IO)

    async def lock_lost(message):
        raise RuntimeError("MessageLockLostError")

    receiver.complete = lock_lost  # type: ignore[method-assign]
    worker = Worker(
        receiver=receiver,
        executor=harness.executor,
        telemetry=harness.telemetry,
        max_wait_seconds=0,
    )

    summary = await worker.run(exit_when_idle=True)

    assert (summary.executed, summary.settle_failures) == (1, 1)
    assert harness.snapshots.all()


async def test_unregistered_stage_pauses_instead_of_spinning(harness: Harness) -> None:
    spec = JobSpec.create(
        stage=StageName.NORMALIZE, pool=WorkerPool.CPU, unit_id="u1", stage_version="1"
    )
    await harness.ledger.enqueue([spec], now=harness.clock.now())
    [event] = await harness.ledger.lease_batch(now=harness.clock.now(), limit=1, claim_seconds=30)

    await harness.executor.process(event.envelope)

    job = await harness.ledger.get(spec.job_id)
    assert job is not None
    assert (job.status, job.last_error_category) == (
        JobStatus.RETRY_WAIT,
        ErrorCategory.CONFIGURATION,
    )
    assert job.not_before == harness.clock.now() + timedelta(seconds=60)


async def test_envelope_must_match_the_job_it_names(harness: Harness) -> None:
    spec = await harness.plan_snapshot(BLOB, b"data")
    [event] = await harness.ledger.lease_batch(now=harness.clock.now(), limit=1, claim_seconds=30)
    forged = event.envelope.model_copy(update={"fingerprint": "f" * 64})

    result = await harness.executor.process(forged)

    assert result.reason == "envelope_mismatch"
    job = await harness.ledger.get(spec.job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING


def test_registry_and_planner_reject_inconsistent_configuration(harness: Harness) -> None:
    with pytest.raises(ConfigurationError, match="already registered"):
        harness.registry.register(StageName.SNAPSHOT, harness.snapshot_handler)
    with pytest.raises(ConfigurationError, match="No handler"):
        StageRegistry().get(StageName.EMBED)
    with pytest.raises(ConfigurationError, match="No handler version"):
        JobPlanner(versions={}).spec(StageName.EMBED, unit_id="u")
    assert harness.registry.pool_for(StageName.RENDER) is WorkerPool.HEAVY


def test_services_report_missing_dependencies(harness: Harness) -> None:
    services = StageServices(clock=harness.clock, planner=harness.services.planner)
    for require in (
        services.require_sources,
        services.require_snapshots,
        services.require_artifacts,
    ):
        with pytest.raises(ConfigurationError):
            require()


async def test_dispatcher_groups_events_by_pool(harness: Harness) -> None:
    await harness.plan_snapshot(BLOB, b"data")
    harness.registry.register(StageName.CHUNK, harness.snapshot_handler)
    chunk = harness.registry.planner(max_failures=3).spec(StageName.CHUNK, unit_id="u")
    await harness.ledger.enqueue([chunk], now=harness.clock.now())
    dispatcher: OutboxDispatcher = harness.dispatcher()

    assert await dispatcher.dispatch_once() == 2
    assert (harness.bus.depth(WorkerPool.IO), harness.bus.depth(WorkerPool.CPU)) == (1, 1)
