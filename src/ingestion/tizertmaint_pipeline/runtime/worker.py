"""Queue consumer: validate envelope, execute via the ledger, then settle the message."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Generic

from ..domain.envelope import WorkEnvelope
from ..domain.errors import InvalidInputError
from ..ports.messaging import MessageReceiver, MessageT
from ..ports.telemetry import Telemetry
from .executor import Disposition, MessageResult, StageExecutor

logger = logging.getLogger(__name__)


@dataclass
class WorkerSummary:
    received: int = 0
    executed: int = 0
    completed: int = 0
    dead_lettered: int = 0
    abandoned: int = 0
    settle_failures: int = 0


class Worker(Generic[MessageT]):
    """Settles only after the ledger commit, so a lost settlement merely causes a cheap no-op."""

    def __init__(
        self,
        *,
        receiver: MessageReceiver[MessageT],
        executor: StageExecutor,
        telemetry: Telemetry,
        max_wait_seconds: float,
        concurrency: int = 1,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._receiver = receiver
        self._executor = executor
        self._telemetry = telemetry
        self._max_wait_seconds = max_wait_seconds
        self._concurrency = concurrency
        self.summary = WorkerSummary()

    async def run(
        self,
        *,
        stop: asyncio.Event | None = None,
        max_messages: int | None = None,
        exit_when_idle: bool = False,
    ) -> WorkerSummary:
        stop = stop or asyncio.Event()
        in_flight: set[asyncio.Task[MessageResult]] = set()
        try:
            while not stop.is_set():
                remaining = None if max_messages is None else max_messages - self.summary.received
                if remaining is not None and remaining <= 0:
                    break
                free = self._concurrency - len(in_flight)
                if free == 0:
                    await self._reap(in_flight, wait=True)
                    continue
                batch = free if remaining is None else min(free, remaining)
                messages = await self._receiver.receive(
                    max_messages=batch, max_wait_seconds=self._max_wait_seconds
                )
                if not messages:
                    if in_flight:
                        await self._reap(in_flight, wait=True)
                        continue
                    if exit_when_idle:
                        break
                    continue
                self.summary.received += len(messages)
                for message in messages:
                    in_flight.add(asyncio.create_task(self.handle(message)))
                await self._reap(in_flight, wait=False)
            while in_flight:
                await self._reap(in_flight, wait=True)
        finally:
            for task in in_flight:
                task.cancel()
        return self.summary

    async def _reap(self, in_flight: set[asyncio.Task[MessageResult]], *, wait: bool) -> None:
        if not in_flight:
            return
        if wait:
            done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
        else:
            done = {task for task in in_flight if task.done()}
        for task in done:
            in_flight.discard(task)
            task.result()  # re-raise BaseExceptions such as cancellation

    async def handle(self, message: MessageT) -> MessageResult:
        try:
            envelope = WorkEnvelope.parse(message.body)
        except InvalidInputError as error:
            result = MessageResult(Disposition.DEAD_LETTER, f"invalid_envelope: {error}")
        else:
            try:
                result = await self._executor.process(envelope)
            except Exception:
                logger.exception(
                    "Infrastructure failure while processing; returning message to the queue",
                    extra={"job_id": envelope.job_id, "stage": envelope.stage.value},
                )
                result = MessageResult(Disposition.ABANDON, "infrastructure_failure")
        if result.executed:
            self.summary.executed += 1
        await self._settle(message, result)
        return result

    async def _settle(self, message: MessageT, result: MessageResult) -> None:
        try:
            if result.disposition is Disposition.COMPLETE:
                await self._receiver.complete(message)
                self.summary.completed += 1
            elif result.disposition is Disposition.DEAD_LETTER:
                await self._receiver.dead_letter(
                    message,
                    reason=(result.reason or "rejected")[:128],
                    description=result.reason or "",
                )
                self.summary.dead_lettered += 1
            else:
                await self._receiver.abandon(message)
                self.summary.abandoned += 1
        except Exception:
            # Lock already lost: redelivery is harmless because the ledger decides the work.
            logger.warning("Message settlement failed", exc_info=True)
            self.summary.settle_failures += 1
            self._telemetry.counter("pipeline.settle_failures")
