"""Execute one stage job under a fenced, heartbeated lease and commit the outcome."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from random import Random
from typing import Any

from ..domain.envelope import WorkEnvelope
from ..domain.errors import LeaseLostError, classify
from ..domain.models import Lease, StageOutcome
from ..domain.retry import RetryPolicy, failure_outcome
from ..faults import FaultPoint
from ..ports.ledger import JobLedger
from ..ports.telemetry import Telemetry
from ..stages.base import StageContext, StageServices
from .registry import StageRegistry

logger = logging.getLogger(__name__)


class Disposition(StrEnum):
    COMPLETE = "complete"
    DEAD_LETTER = "dead_letter"
    ABANDON = "abandon"


@dataclass(frozen=True)
class MessageResult:
    disposition: Disposition
    reason: str | None = None
    executed: bool = False


@dataclass(frozen=True)
class ExecutorSettings:
    worker_id: str
    lease_seconds: float
    heartbeat_seconds: float
    stage_timeout_seconds: float


class StageExecutor:
    def __init__(
        self,
        *,
        ledger: JobLedger,
        registry: StageRegistry,
        services: StageServices,
        policy: RetryPolicy,
        settings: ExecutorSettings,
        telemetry: Telemetry,
        rng: Random | None = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._services = services
        self._policy = policy
        self._settings = settings
        self._telemetry = telemetry
        self._rng = rng or Random()

    async def process(self, envelope: WorkEnvelope) -> MessageResult:
        """Run the job named by ``envelope`` at most once per lease; never raises for job errors."""

        job = await self._ledger.get(envelope.job_id)
        if job is None:
            return MessageResult(Disposition.DEAD_LETTER, "unknown_job")
        if (job.stage, job.unit_id, job.fingerprint) != (
            envelope.stage,
            envelope.unit_id,
            envelope.fingerprint,
        ):
            return MessageResult(Disposition.DEAD_LETTER, "envelope_mismatch")

        clock = self._services.clock
        lease = await self._ledger.claim(
            job.job_id,
            owner=self._settings.worker_id,
            lease_seconds=self._settings.lease_seconds,
            now=clock.now(),
        )
        if lease is None:
            # Done, leased elsewhere or not yet due: the ledger and reconciler own recovery.
            return MessageResult(Disposition.COMPLETE, f"not_claimable:{job.status.value}")

        faults = self._services.faults
        faults.hit(FaultPoint.AFTER_CLAIM)
        started = time.perf_counter()
        try:
            outcome = await self._execute(lease)
            faults.hit(FaultPoint.BEFORE_COMMIT)
            await self._ledger.complete(lease, outcome, now=clock.now())
        except LeaseLostError:
            logger.warning("Lease lost before commit", extra={"job_id": job.job_id})
            self._telemetry.counter("pipeline.lease_lost", stage=job.stage.value)
            return MessageResult(Disposition.COMPLETE, "lease_lost", executed=True)
        self._telemetry.stage_finished(
            stage=job.stage.value,
            status=outcome.status.value,
            duration_seconds=time.perf_counter() - started,
            error_category=outcome.error_category.value if outcome.error_category else None,
        )
        faults.hit(FaultPoint.AFTER_COMMIT_BEFORE_SETTLE)
        return MessageResult(Disposition.COMPLETE, executed=True)

    async def _execute(self, lease: Lease) -> StageOutcome:
        job = lease.job
        try:
            registration = self._registry.get(job.stage)
            timeout = registration.timeout_seconds or self._settings.stage_timeout_seconds
            context = StageContext(services=self._services, lease=lease)
            with self._telemetry.span(
                f"stage.{job.stage.value}",
                job_id=job.job_id,
                unit_id=job.unit_id,
                attempt=job.attempt,
                pool=job.pool.value,
            ):
                return await self._run_heartbeated(
                    lease, registration.handler.execute(context, job), timeout
                )
        except LeaseLostError:
            raise
        except Exception as error:
            category, retry_after = classify(error)
            logger.warning(
                "Stage attempt failed",
                extra={
                    "job_id": job.job_id,
                    "stage": job.stage.value,
                    "error_category": category.value,
                    "error_type": type(error).__name__,
                },
            )
            return failure_outcome(
                category=category,
                message=f"{type(error).__name__}: {error}",
                retry_after=retry_after,
                failure_count=job.failure_count,
                max_failures=job.max_failures,
                now=self._services.clock.now(),
                policy=self._policy,
                rng=self._rng,
            )

    async def _run_heartbeated(
        self, lease: Lease, work: Coroutine[Any, Any, StageOutcome], deadline_seconds: float
    ) -> StageOutcome:
        task = asyncio.ensure_future(work)
        lost = asyncio.Event()

        async def beat() -> None:
            current = lease
            while True:
                await asyncio.sleep(self._settings.heartbeat_seconds)
                try:
                    current = await self._ledger.heartbeat(
                        current,
                        lease_seconds=self._settings.lease_seconds,
                        now=self._services.clock.now(),
                    )
                except LeaseLostError:
                    lost.set()
                    task.cancel()
                    return
                except Exception:
                    logger.warning("Heartbeat failed; retrying", exc_info=True)

        beater = asyncio.create_task(beat())
        try:
            return await asyncio.wait_for(task, timeout=deadline_seconds)
        except asyncio.CancelledError:
            if lost.is_set():
                raise LeaseLostError(f"Lease lost during job {lease.job.job_id}") from None
            raise
        finally:
            beater.cancel()
            with suppress(asyncio.CancelledError):
                await beater
