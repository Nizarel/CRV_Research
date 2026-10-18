"""Outbox dispatcher and lease reconciler, run together by the coordinator."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from random import Random

from ..domain.models import OutboxEvent
from ..domain.retry import RetryPolicy
from ..domain.states import StageName, WorkerPool
from ..faults import NO_FAULTS, FaultInjector, FaultPoint
from ..ports.clock import Clock
from ..ports.ledger import JobLedger, Outbox
from ..ports.messaging import MessageSender
from ..ports.telemetry import Telemetry

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    """Send due outbox events; mark them sent only after the broker accepted them."""

    def __init__(
        self,
        *,
        outbox: Outbox,
        sender: MessageSender,
        clock: Clock,
        telemetry: Telemetry,
        batch_size: int,
        claim_seconds: float,
        retry_policy: RetryPolicy,
        faults: FaultInjector = NO_FAULTS,
        rng: Random | None = None,
    ) -> None:
        self._outbox = outbox
        self._sender = sender
        self._clock = clock
        self._telemetry = telemetry
        self._batch_size = batch_size
        self._claim_seconds = claim_seconds
        self._retry_policy = retry_policy
        self._faults = faults
        self._rng = rng or Random()

    async def dispatch_once(self) -> int:
        events = await self._outbox.lease_batch(
            now=self._clock.now(), limit=self._batch_size, claim_seconds=self._claim_seconds
        )
        by_pool: dict[WorkerPool, list[OutboxEvent]] = defaultdict(list)
        for event in events:
            by_pool[event.pool].append(event)
        sent = 0
        for pool, group in by_pool.items():
            event_ids = [event.event_id for event in group]
            try:
                await self._sender.send(pool, [event.envelope for event in group])
            except Exception as error:
                delay = self._retry_policy.backoff_seconds(1, retry_after=None, rng=self._rng)
                logger.warning(
                    "Outbox send failed; events released",
                    extra={
                        "pool": pool.value,
                        "events": len(group),
                        "error_type": type(error).__name__,
                    },
                )
                await self._outbox.release(
                    event_ids,
                    error=f"{type(error).__name__}: {error}",
                    retry_at=self._clock.now() + timedelta(seconds=delay),
                )
                self._telemetry.counter(
                    "pipeline.outbox.send_failures", len(group), pool=pool.value
                )
                continue
            self._faults.hit(FaultPoint.AFTER_SEND_BEFORE_MARK)
            await self._outbox.mark_sent(event_ids, now=self._clock.now())
            self._telemetry.counter("pipeline.outbox.sent", len(group), pool=pool.value)
            sent += len(group)
        return sent


@dataclass(frozen=True)
class ReconcileResult:
    requeued: int
    failed: int
    redispatched: int
    released: int = 0
    woken: int = 0

    @property
    def total(self) -> int:
        return self.requeued + self.failed + self.redispatched + self.released + self.woken


class LeaseReconciler:
    """Recover expired leases, re-wake due jobs whose messages were lost and fill free slots."""

    def __init__(
        self,
        *,
        ledger: JobLedger,
        clock: Clock,
        telemetry: Telemetry,
        batch_size: int,
        stale_dispatch_seconds: float,
        remote_limits: Mapping[StageName, int] | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._telemetry = telemetry
        self._batch_size = batch_size
        self._stale = timedelta(seconds=stale_dispatch_seconds)
        self._remote_limits = dict(remote_limits or {})

    async def reconcile_once(self) -> ReconcileResult:
        now = self._clock.now()
        recovery = await self._ledger.recover_expired(now=now, limit=self._batch_size)
        redispatched = await self._ledger.redispatch_stale(
            now=now, stale_before=now - self._stale, limit=self._batch_size
        )
        released = await self._ledger.release_ready_barriers(now=now, limit=self._batch_size)
        woken = 0
        for submit, limit in self._remote_limits.items():
            woken += await self._ledger.wake_parked(submit, limit=limit, now=now)
        result = ReconcileResult(recovery.requeued, recovery.failed, redispatched, released, woken)
        if result.total:
            logger.info(
                "Reconciled jobs",
                extra={
                    "requeued": result.requeued,
                    "failed": result.failed,
                    "redispatched": result.redispatched,
                    "released": result.released,
                    "woken": result.woken,
                },
            )
            self._telemetry.counter("pipeline.leases.recovered", result.requeued)
            self._telemetry.counter("pipeline.leases.exhausted", result.failed)
            self._telemetry.counter("pipeline.jobs.redispatched", result.redispatched)
            self._telemetry.counter("pipeline.barriers.released", result.released)
            self._telemetry.counter("pipeline.submits.woken", result.woken)
        return result


class Coordinator:
    """Runs dispatcher and reconciler loops until ``stop`` is set."""

    def __init__(
        self,
        *,
        dispatcher: OutboxDispatcher,
        reconciler: LeaseReconciler,
        dispatch_interval_seconds: float,
        reconcile_interval_seconds: float,
        dispatch_batch_size: int,
    ) -> None:
        self._dispatcher = dispatcher
        self._reconciler = reconciler
        self._dispatch_interval = dispatch_interval_seconds
        self._reconcile_interval = reconcile_interval_seconds
        self._batch_size = dispatch_batch_size

    async def run(self, stop: asyncio.Event) -> None:
        async def reconcile() -> int:
            return (await self._reconciler.reconcile_once()).total

        await asyncio.gather(
            _loop(stop, self._dispatcher.dispatch_once, self._dispatch_interval, self._batch_size),
            _loop(stop, reconcile, self._reconcile_interval, None),
        )


async def _loop(
    stop: asyncio.Event,
    step: Callable[[], Awaitable[int]],
    interval: float,
    busy_threshold: int | None,
) -> None:
    while not stop.is_set():
        try:
            count = await step()
        except Exception:
            logger.exception("Coordinator step failed")
            count = 0
        if busy_threshold is not None and count >= busy_threshold:
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
