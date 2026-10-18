"""In-memory peek-lock queue with Service Bus delivery semantics."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from ...domain.envelope import WorkEnvelope
from ...domain.states import WorkerPool


@dataclass
class InMemoryMessage:
    body: bytes
    message_id: str | None
    delivery_count: int = 0


class InMemoryBus:
    """Sender for every pool; ``receiver(pool)`` returns that queue's peek-lock receiver."""

    def __init__(self, *, max_delivery_count: int = 10) -> None:
        self.max_delivery_count = max_delivery_count
        self.queues: dict[WorkerPool, deque[InMemoryMessage]] = {
            pool: deque() for pool in WorkerPool
        }
        self.locked: dict[WorkerPool, list[InMemoryMessage]] = {pool: [] for pool in WorkerPool}
        self.dead_letters: dict[WorkerPool, list[tuple[InMemoryMessage, str]]] = {
            pool: [] for pool in WorkerPool
        }
        self.sent: list[WorkEnvelope] = []
        self.fail_next_sends = 0

    async def send(self, pool: WorkerPool, envelopes: Sequence[WorkEnvelope]) -> None:
        if self.fail_next_sends:
            self.fail_next_sends -= 1
            raise ConnectionError("Simulated broker send failure")
        for envelope in envelopes:
            self.queues[pool].append(InMemoryMessage(envelope.to_bytes(), envelope.event_id))
            self.sent.append(envelope)

    def inject(self, pool: WorkerPool, body: bytes, *, message_id: str | None = None) -> None:
        self.queues[pool].append(InMemoryMessage(body, message_id))

    def expire_locks(self, pool: WorkerPool | None = None) -> int:
        """Simulate lock expiry after a crashed consumer: locked messages become visible."""

        pools = [pool] if pool is not None else list(WorkerPool)
        released = 0
        for current in pools:
            while self.locked[current]:
                self.queues[current].append(self.locked[current].pop())
                released += 1
        return released

    def depth(self, pool: WorkerPool) -> int:
        return len(self.queues[pool])

    def receiver(self, pool: WorkerPool) -> InMemoryReceiver:
        return InMemoryReceiver(self, pool)


class InMemoryReceiver:
    def __init__(self, bus: InMemoryBus, pool: WorkerPool) -> None:
        self._bus = bus
        self._pool = pool

    async def receive(self, *, max_messages: int, max_wait_seconds: float) -> list[InMemoryMessage]:
        queue = self._bus.queues[self._pool]
        received: list[InMemoryMessage] = []
        while queue and len(received) < max_messages:
            message = queue.popleft()
            message.delivery_count += 1
            if message.delivery_count > self._bus.max_delivery_count:
                self._bus.dead_letters[self._pool].append((message, "MaxDeliveryCountExceeded"))
                continue
            self._bus.locked[self._pool].append(message)
            received.append(message)
        return received

    def _unlock(self, message: InMemoryMessage) -> None:
        self._bus.locked[self._pool].remove(message)

    async def complete(self, message: InMemoryMessage) -> None:
        self._unlock(message)

    async def abandon(self, message: InMemoryMessage) -> None:
        self._unlock(message)
        self._bus.queues[self._pool].append(message)

    async def dead_letter(self, message: InMemoryMessage, *, reason: str, description: str) -> None:
        self._unlock(message)
        self._bus.dead_letters[self._pool].append((message, reason))
