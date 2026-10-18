"""Queue transport ports. Delivery is at-least-once; the ledger provides idempotency."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from ..domain.envelope import WorkEnvelope
from ..domain.states import WorkerPool


class ReceivedMessage(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def message_id(self) -> str | None: ...

    @property
    def delivery_count(self) -> int: ...


MessageT = TypeVar("MessageT", bound=ReceivedMessage)


class MessageSender(Protocol):
    async def send(self, pool: WorkerPool, envelopes: Sequence[WorkEnvelope]) -> None:
        """Return only after the broker accepted every envelope."""
        ...


class MessageReceiver(Protocol[MessageT]):
    """Settlement methods accept only messages this receiver produced."""

    async def receive(self, *, max_messages: int, max_wait_seconds: float) -> list[MessageT]: ...

    async def complete(self, message: MessageT) -> None: ...

    async def abandon(self, message: MessageT) -> None: ...

    async def dead_letter(self, message: MessageT, *, reason: str, description: str) -> None: ...
