"""Service Bus sender and peek-lock receiver.

The async SDK clients are not coroutine-safe, so every call on a sender or receiver is
serialized with an ``asyncio.Lock``. Receivers must be created with ``prefetch_count=0``:
prefetched messages keep their lock clock running while they wait in the local buffer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.envelope import WorkEnvelope
from ...domain.states import WorkerPool
from ..azure_errors import translate_azure_error


def open_client(fully_qualified_namespace: str, credential: Any) -> Any:
    from azure.servicebus.aio import ServiceBusClient

    return ServiceBusClient(fully_qualified_namespace, credential)


def _message(envelope: WorkEnvelope) -> Any:
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(
        envelope.to_bytes(),
        message_id=envelope.event_id,
        content_type="application/json",
        subject=envelope.stage.value,
        application_properties={
            "schema_version": envelope.schema_version,
            "job_id": envelope.job_id,
        },
    )


class ServiceBusSender:
    """``MessageSender`` routing each pool to its queue; ``message_id`` is the event ID."""

    def __init__(self, client: Any, queues: Mapping[WorkerPool, str]) -> None:
        self._client = client
        self._queues = dict(queues)
        self._senders: dict[WorkerPool, Any] = {}
        self._locks = {pool: asyncio.Lock() for pool in WorkerPool}

    async def send(self, pool: WorkerPool, envelopes: Sequence[WorkEnvelope]) -> None:
        from azure.servicebus.exceptions import MessageSizeExceededError

        if not envelopes:
            return
        async with self._locks[pool]:
            sender = self._senders.get(pool)
            if sender is None:
                sender = self._client.get_queue_sender(queue_name=self._queues[pool])
                self._senders[pool] = sender
            try:
                batch = await sender.create_message_batch()
                for envelope in envelopes:
                    message = _message(envelope)
                    try:
                        batch.add_message(message)
                    except MessageSizeExceededError:
                        if len(batch) == 0:
                            raise
                        await sender.send_messages(batch)
                        batch = await sender.create_message_batch()
                        batch.add_message(message)
                if len(batch):
                    await sender.send_messages(batch)
            except Exception as error:
                raise (translate_azure_error(error) or error) from error

    async def close(self) -> None:
        for pool, sender in list(self._senders.items()):
            async with self._locks[pool]:
                await sender.close()
        self._senders.clear()


@dataclass(frozen=True)
class ServiceBusReceived:
    raw: Any

    @property
    def body(self) -> bytes:
        body = self.raw.body
        if isinstance(body, bytes | bytearray):
            return bytes(body)
        return b"".join(bytes(section) for section in body)

    @property
    def message_id(self) -> str | None:
        value = self.raw.message_id
        return None if value is None else str(value)

    @property
    def delivery_count(self) -> int:
        return int(self.raw.delivery_count or 0)


class ServiceBusMessageReceiver:
    """``MessageReceiver`` over one queue receiver with optional lock auto-renewal."""

    def __init__(
        self,
        receiver: Any,
        *,
        renewer: Any | None = None,
        max_lock_renewal_seconds: float = 600.0,
    ) -> None:
        self._receiver = receiver
        self._renewer = renewer
        self._max_lock_renewal_seconds = max_lock_renewal_seconds
        self._lock = asyncio.Lock()

    async def receive(
        self, *, max_messages: int, max_wait_seconds: float
    ) -> list[ServiceBusReceived]:
        async with self._lock:
            messages = await self._receiver.receive_messages(
                max_message_count=max_messages, max_wait_time=max_wait_seconds
            )
        if self._renewer is not None:
            for message in messages:
                self._renewer.register(
                    self._receiver,
                    message,
                    max_lock_renewal_duration=self._max_lock_renewal_seconds,
                )
        return [ServiceBusReceived(message) for message in messages]

    async def complete(self, message: ServiceBusReceived) -> None:
        async with self._lock:
            await self._receiver.complete_message(message.raw)

    async def abandon(self, message: ServiceBusReceived) -> None:
        async with self._lock:
            await self._receiver.abandon_message(message.raw)

    async def dead_letter(
        self, message: ServiceBusReceived, *, reason: str, description: str
    ) -> None:
        async with self._lock:
            await self._receiver.dead_letter_message(
                message.raw, reason=reason, error_description=description[:1024]
            )


def open_receiver(
    client: Any, queue_name: str, *, max_lock_renewal_seconds: float
) -> tuple[Any, Any]:
    """Return ``(sdk_receiver, renewer)``; the renewer MUST be the ``aio`` AutoLockRenewer."""

    from azure.servicebus import ServiceBusReceiveMode
    from azure.servicebus.aio import AutoLockRenewer

    receiver = client.get_queue_receiver(
        queue_name=queue_name,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        prefetch_count=0,
    )
    renewer = AutoLockRenewer(max_lock_renewal_duration=max_lock_renewal_seconds)
    return receiver, renewer
