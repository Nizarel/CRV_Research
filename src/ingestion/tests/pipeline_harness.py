"""In-memory pipeline harness shared by unit, contract and end-to-end tests."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from random import Random
from typing import Any

from tizertmaint_pipeline.adapters.memory.bus import InMemoryBus
from tizertmaint_pipeline.adapters.memory.clock import ManualClock
from tizertmaint_pipeline.adapters.memory.ledger import InMemoryLedger, InMemorySnapshots
from tizertmaint_pipeline.adapters.memory.storage import (
    InMemoryArtifactStore,
    InMemorySourceReader,
)
from tizertmaint_pipeline.adapters.memory.telemetry_recorder import RecordingTelemetry
from tizertmaint_pipeline.domain.models import JobSpec, StageJob, StageOutcome
from tizertmaint_pipeline.domain.retry import RetryPolicy
from tizertmaint_pipeline.domain.states import StageName, WorkerPool
from tizertmaint_pipeline.faults import FaultInjector, FaultPoint
from tizertmaint_pipeline.runtime.coordinator import LeaseReconciler, OutboxDispatcher
from tizertmaint_pipeline.runtime.executor import ExecutorSettings, StageExecutor
from tizertmaint_pipeline.runtime.registry import StageRegistry
from tizertmaint_pipeline.runtime.worker import Worker
from tizertmaint_pipeline.stages.base import StageContext, StageHandler, StageServices
from tizertmaint_pipeline.stages.snapshot import SnapshotStage, snapshot_job

START = datetime(2026, 1, 1, tzinfo=UTC)
LEASE_SECONDS = 60.0


class CountingHandler:
    """Wraps a handler and counts executions."""

    def __init__(self, inner: StageHandler) -> None:
        self.inner = inner
        self.calls = 0

    @property
    def version(self) -> str:
        return self.inner.version

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        self.calls += 1
        return await self.inner.execute(context, job)


class FunctionStage:
    """Test stage whose behaviour is a coroutine function."""

    def __init__(
        self,
        behaviour: Callable[[StageContext, StageJob], Awaitable[StageOutcome]],
        version: str = "1",
    ) -> None:
        self._behaviour = behaviour
        self._version = version
        self.calls = 0

    @property
    def version(self) -> str:
        return self._version

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        self.calls += 1
        return await self._behaviour(context, job)


@dataclass
class Harness:
    clock: ManualClock
    ledger: InMemoryLedger
    snapshots: InMemorySnapshots
    sources: InMemorySourceReader
    artifacts: InMemoryArtifactStore
    bus: InMemoryBus
    telemetry: RecordingTelemetry
    faults: FaultInjector
    registry: StageRegistry
    services: StageServices
    executor: StageExecutor
    snapshot_handler: CountingHandler
    policy: RetryPolicy
    workers: dict[WorkerPool, Worker] = field(default_factory=dict)

    def worker(self, pool: WorkerPool = WorkerPool.IO, *, concurrency: int = 1) -> Worker:
        if pool not in self.workers:
            self.workers[pool] = Worker(
                receiver=self.bus.receiver(pool),
                executor=self.executor,
                telemetry=self.telemetry,
                max_wait_seconds=0,
                concurrency=concurrency,
            )
        return self.workers[pool]

    def dispatcher(self) -> OutboxDispatcher:
        return OutboxDispatcher(
            outbox=self.ledger,
            sender=self.bus,
            clock=self.clock,
            telemetry=self.telemetry,
            batch_size=100,
            claim_seconds=30,
            retry_policy=self.policy,
            faults=self.faults,
            rng=Random(1),
        )

    def reconciler(self, *, stale_seconds: float = 900) -> LeaseReconciler:
        return LeaseReconciler(
            ledger=self.ledger,
            clock=self.clock,
            telemetry=self.telemetry,
            batch_size=100,
            stale_dispatch_seconds=stale_seconds,
        )

    def put_source(self, blob_name: str, data: bytes) -> str:
        return self.sources.put_blob(blob_name, data)

    def snapshot_spec(
        self, blob_name: str, *, etag: str, content_hash: str | None = None
    ) -> JobSpec:
        return snapshot_job(
            self.services.planner,
            container_name=self.sources.container_name,
            container_url=self.sources.container_url,
            blob_name=blob_name,
            etag=etag,
            content_hash=content_hash,
        )

    async def plan_snapshot(self, blob_name: str, data: bytes) -> JobSpec:
        etag = self.put_source(blob_name, data)
        spec = self.snapshot_spec(
            blob_name, etag=etag, content_hash=hashlib.sha256(data).hexdigest()
        )
        await self.ledger.enqueue([spec], now=self.clock.now())
        return spec

    async def drain(
        self, *, max_rounds: int = 50, advance_time: bool = True, strict: bool = True
    ) -> None:
        """Dispatch and consume until quiet, jumping the clock to the next scheduled event."""

        dispatcher = self.dispatcher()
        for _ in range(max_rounds):
            sent = await dispatcher.dispatch_once()
            received = 0
            for pool in WorkerPool:
                if self.bus.depth(pool):
                    before = self.worker(pool).summary.received
                    await self.worker(pool).run(exit_when_idle=True)
                    received += self.worker(pool).summary.received - before
            if sent or received:
                continue
            pending = [
                row.event.available_at for row in self.ledger.outbox_rows() if row.sent_at is None
            ]
            if not advance_time or not pending:
                return
            self.clock.advance(max((min(pending) - self.clock.now()).total_seconds(), 0) + 1)
        if strict:
            raise AssertionError("Pipeline did not settle")


HarnessFactory = Callable[..., Harness]


def build_harness(
    *,
    faults: dict[FaultPoint, int] | None = None,
    stages: dict[StageName, StageHandler] | None = None,
    max_failures: int = 3,
    max_source_bytes: int = 1024 * 1024,
    successors: Any = None,
    stage_timeout_seconds: float = 30.0,
    heartbeat_seconds: float = 10.0,
    extra_services: dict[str, Any] | None = None,
) -> Harness:
    clock = ManualClock(START)
    ledger = InMemoryLedger()
    snapshots = InMemorySnapshots()
    sources = InMemorySourceReader(chunk_size=7)
    artifacts = InMemoryArtifactStore()
    injector = FaultInjector(faults)
    telemetry = RecordingTelemetry()
    snapshot_handler = CountingHandler(
        SnapshotStage(max_source_bytes=max_source_bytes, successors=successors)
    )
    registry = StageRegistry().register(StageName.SNAPSHOT, snapshot_handler)
    for stage, handler in (stages or {}).items():
        registry.register(stage, handler)
    services = StageServices(
        clock=clock,
        planner=registry.planner(max_failures=max_failures),
        faults=injector,
        snapshots=snapshots,
        sources=sources,
        artifacts=artifacts,
        plans=ledger,
        workload=ledger,
        **(extra_services or {}),
    )
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=10, pause_delay_seconds=60)
    executor = StageExecutor(
        ledger=ledger,
        registry=registry,
        services=services,
        policy=policy,
        settings=ExecutorSettings(
            worker_id="worker-test",
            lease_seconds=LEASE_SECONDS,
            heartbeat_seconds=heartbeat_seconds,
            stage_timeout_seconds=stage_timeout_seconds,
        ),
        telemetry=telemetry,
        rng=Random(0),
    )
    return Harness(
        clock=clock,
        ledger=ledger,
        snapshots=snapshots,
        sources=sources,
        artifacts=artifacts,
        bus=InMemoryBus(),
        telemetry=telemetry,
        faults=injector,
        registry=registry,
        services=services,
        executor=executor,
        snapshot_handler=snapshot_handler,
        policy=policy,
    )
