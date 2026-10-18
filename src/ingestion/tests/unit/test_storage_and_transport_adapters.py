from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.servicebus.exceptions import MessageSizeExceededError

from ingest_pipeline.adapters.blob.artifact_store import BlobArtifactStore
from ingest_pipeline.adapters.blob.intake import publish_intake
from ingest_pipeline.adapters.blob.source_reader import BlobSourceReader
from ingest_pipeline.adapters.local_intake import CorpusIntake
from ingest_pipeline.adapters.memory.storage import InMemoryArtifactStore
from ingest_pipeline.adapters.servicebus.transport import (
    ServiceBusMessageReceiver,
    ServiceBusReceived,
    ServiceBusSender,
)
from ingest_pipeline.domain.envelope import WorkEnvelope
from ingest_pipeline.domain.errors import (
    IntegrityError,
    InvalidInputError,
    StaleSourceError,
)
from ingest_pipeline.domain.intake import IntakeLimits
from ingest_pipeline.domain.states import StageName, WorkerPool


class _Downloader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def chunks(self):
        for start in range(0, len(self._data), 3):
            yield self._data[start : start + 3]

    async def readall(self) -> bytes:
        return self._data


class _BlobClient:
    def __init__(self, store: dict[str, dict[str, Any]], name: str) -> None:
        self._store = store
        self._name = name
        self.download_kwargs: dict[str, Any] = {}

    async def get_blob_properties(self):
        blob = self._store.get(self._name)
        if blob is None:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(
            etag=blob["etag"],
            size=len(blob["data"]),
            metadata=blob.get("metadata", {}),
            content_settings=SimpleNamespace(content_type="application/pdf"),
            last_modified=None,
            version_id=None,
        )

    async def download_blob(self, **kwargs: Any):
        self.download_kwargs = kwargs
        blob = self._store.get(self._name)
        if blob is None:
            raise ResourceNotFoundError("missing")
        if "etag" in kwargs and kwargs["etag"] != blob["etag"]:
            raise ResourceModifiedError("condition not met")
        return _Downloader(blob["data"])

    async def upload_blob(
        self, data: bytes | IO[bytes], *, overwrite: bool, content_settings: Any,
        metadata: dict, **kwargs: Any,
    ):
        previous = self._store.get(self._name)
        if "etag" in kwargs and (previous is None or previous["etag"] != kwargs["etag"]):
            raise ResourceModifiedError("condition not met")
        if not overwrite and self._name in self._store:
            raise ResourceExistsError("exists")
        payload = data if isinstance(data, bytes) else data.read()
        if "length" in kwargs:
            assert len(payload) == kwargs["length"]
        version = int(previous["etag"].strip('"')) + 1 if previous else 1
        self._store[self._name] = {
            "data": payload, "etag": f'"{version}"', "metadata": metadata,
        }


class _Container:
    container_name = "content"
    url = "https://account.blob.core.windows.net/content"

    def __init__(self) -> None:
        self.blobs: dict[str, dict[str, Any]] = {}

    def get_blob_client(self, name: str) -> _BlobClient:
        return _BlobClient(self.blobs, name)


async def test_source_reader_reads_exact_etag_and_reports_changes() -> None:
    container = _Container()
    container.blobs["a.pdf"] = {
        "data": b"0123456789",
        "etag": '"v1"',
        "metadata": {"content_hash": "x"},
    }
    reader = BlobSourceReader(container)

    properties = await reader.properties("a.pdf")
    assert (properties.etag, properties.size_bytes, properties.declared_content_hash) == (
        '"v1"',
        10,
        "x",
    )
    assert b"".join([chunk async for chunk in reader.stream("a.pdf", etag='"v1"')]) == b"0123456789"

    with pytest.raises(StaleSourceError, match="changed"):
        [chunk async for chunk in reader.stream("a.pdf", etag='"v0"')]
    with pytest.raises(InvalidInputError):
        await reader.properties("missing.pdf")
    with pytest.raises(StaleSourceError, match="removed"):
        [chunk async for chunk in reader.stream("missing.pdf", etag='"v1"')]


async def test_artifact_store_is_write_once_and_verifies_existing_objects() -> None:
    container = _Container()
    store = BlobArtifactStore(container, max_bytes=100)

    ref = await store.put(b"payload", media_type="application/json", schema_name="raw/v1")
    again = await store.put(b"payload", media_type="application/json")
    assert ref.sha256 == hashlib.sha256(b"payload").hexdigest() == again.sha256
    assert ref.uri == f"sha256/{ref.sha256[:2]}/{ref.sha256}"
    assert await store.get(ref) == b"payload"

    container.blobs[ref.uri]["metadata"] = {}
    container.blobs[ref.uri]["data"] = b"tampere"
    with pytest.raises(IntegrityError):
        await store.put(b"payload", media_type="application/json")
    with pytest.raises(IntegrityError):
        await store.get(ref)
    with pytest.raises(InvalidInputError):
        await store.put(b"x" * 101, media_type="application/octet-stream")


async def test_memory_artifact_store_matches_blob_semantics() -> None:
    store = InMemoryArtifactStore(max_bytes=10)
    ref = await store.put(b"abc", media_type="text/plain")
    assert await store.get(ref) == b"abc"
    store.objects[ref.uri] = b"abd"
    with pytest.raises(IntegrityError):
        await store.get(ref)


async def test_intake_publication_is_idempotent_and_retains_provenance(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source = root / "values.csv"
    source.write_bytes(b"Sample,Cu\nA,12.5\n")
    output = tmp_path / "intake"
    intake = CorpusIntake(output, IntakeLimits())
    report = intake.prepare(root)
    container = _Container()

    async def publish():
        return [item async for item in publish_intake(
            container, output / "manifest.json", prefix="crv", max_source_bytes=10_000,
        )]

    first = await publish()
    assert await publish() == first
    name, etag, digest = first[0]
    assert name.endswith(".csv")
    properties = await BlobSourceReader(container).properties(name)
    assert properties.provenance is not None
    assert properties.provenance.source_path == "values.csv"
    assert properties.provenance.original_hash == digest == report.items[0].input_hash
    original = properties.provenance.original_blob
    assert container.blobs[original]["data"] == source.read_bytes()

    source.write_bytes(b"Sample,Cu\nA,15.2\n")
    intake.prepare(root)
    changed = await publish()
    assert changed[0][0] == name
    assert changed[0][1] != etag
    assert changed[0][2] != digest
    assert original in container.blobs


@pytest.mark.parametrize("damage", ["incomplete", "failed", "duplicate", "size", "object"])
async def test_bad_intake_is_rejected_before_upload(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_bytes(b"report")
    output = tmp_path / "out"
    report = CorpusIntake(output, IntakeLimits()).prepare(root)
    manifest = output / "manifest.json"
    data = report.model_dump(mode="json")
    if damage == "incomplete":
        data["complete"] = False
    elif damage == "failed":
        data["items"][0]["status"] = "failed"
    elif damage == "duplicate":
        data["items"].append(data["items"][0])
    elif damage == "size":
        data["items"][0]["size_bytes"] += 1
    else:
        (output / "objects" / str(report.items[0].content_hash)).write_bytes(b"tampered")
    manifest.write_text(json.dumps(data))
    container = _Container()
    with pytest.raises((IntegrityError, InvalidInputError)):
        [item async for item in publish_intake(
            container, manifest, prefix="crv", max_source_bytes=10_000,
        )]
    assert not container.blobs


async def test_invalid_source_provenance_is_an_input_error() -> None:
    container = _Container()
    container.blobs["a.pdf"] = {
        "data": b"pdf", "etag": '"1"', "metadata": {"ingest_provenance": "{}"},
    }
    with pytest.raises(InvalidInputError, match="provenance"):
        await BlobSourceReader(container).properties("a.pdf")


class _Batch:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.messages: list[Any] = []

    def add_message(self, message: Any) -> None:
        if len(self.messages) >= self.capacity:
            raise MessageSizeExceededError(message="batch full")
        self.messages.append(message)

    def __len__(self) -> int:
        return len(self.messages)


class _QueueSender:
    def __init__(self) -> None:
        self.sent: list[list[Any]] = []
        self.closed = False

    async def create_message_batch(self) -> _Batch:
        return _Batch(capacity=2)

    async def send_messages(self, batch: _Batch) -> None:
        self.sent.append(list(batch.messages))

    async def close(self) -> None:
        self.closed = True


class _BusClient:
    def __init__(self) -> None:
        self.senders: dict[str, _QueueSender] = {}

    def get_queue_sender(self, *, queue_name: str) -> _QueueSender:
        return self.senders.setdefault(queue_name, _QueueSender())


def _envelope(index: int) -> WorkEnvelope:
    return WorkEnvelope(
        event_id=f"event-{index}",
        job_id=f"job-{index}",
        stage=StageName.SNAPSHOT,
        unit_id="unit",
        fingerprint="d" * 64,
    )


async def test_sender_splits_full_batches_and_uses_event_id_as_message_id() -> None:
    client = _BusClient()
    sender = ServiceBusSender(client, {WorkerPool.IO: "ingest-io"})

    await sender.send(WorkerPool.IO, [_envelope(index) for index in range(5)])

    queue = client.senders["ingest-io"]
    assert [len(batch) for batch in queue.sent] == [2, 2, 1]
    first = queue.sent[0][0]
    assert first.message_id == "event-0"
    assert first.subject == "snapshot"
    assert WorkEnvelope.parse(b"".join(first.body)).job_id == "job-0"
    await sender.close()
    assert queue.closed


class _SdkReceiver:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.settled: list[tuple[str, Any]] = []

    async def receive_messages(self, *, max_message_count: int, max_wait_time: float):
        taken, self.messages = self.messages[:max_message_count], self.messages[max_message_count:]
        return taken

    async def complete_message(self, message: Any) -> None:
        self.settled.append(("complete", message))

    async def abandon_message(self, message: Any) -> None:
        self.settled.append(("abandon", message))

    async def dead_letter_message(
        self, message: Any, *, reason: str, error_description: str
    ) -> None:
        self.settled.append((f"dead_letter:{reason}", message))


class _Renewer:
    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register(self, receiver: Any, message: Any, *, max_lock_renewal_duration: float) -> None:
        self.registered.append((message, max_lock_renewal_duration))


async def test_receiver_wraps_messages_registers_renewal_and_settles() -> None:
    raw = SimpleNamespace(body=iter([b"ab", b"c"]), message_id="m-1", delivery_count=2)
    sdk = _SdkReceiver([raw])
    renewer = _Renewer()
    receiver = ServiceBusMessageReceiver(sdk, renewer=renewer, max_lock_renewal_seconds=120)

    [message] = await receiver.receive(max_messages=5, max_wait_seconds=1)
    assert (message.body, message.message_id, message.delivery_count) == (b"abc", "m-1", 2)
    assert renewer.registered == [(raw, 120)]

    await receiver.complete(message)
    await receiver.dead_letter(message, reason="invalid_envelope", description="bad")
    assert [action for action, _ in sdk.settled] == ["complete", "dead_letter:invalid_envelope"]
    assert (
        ServiceBusReceived(SimpleNamespace(body=b"xyz", message_id=None, delivery_count=None)).body
        == b"xyz"
    )
