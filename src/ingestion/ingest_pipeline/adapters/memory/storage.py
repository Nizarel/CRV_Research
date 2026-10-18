"""In-memory content-addressed artifact store and source reader."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ...domain.errors import IntegrityError, InvalidInputError, StaleSourceError
from ...domain.ids import artifact_key, sha256_hex
from ...domain.models import ArtifactRef
from ...ports.storage import SourceProperties


class InMemoryArtifactStore:
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    async def put(
        self, data: bytes, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        self.put_calls += 1
        if len(data) > self._max_bytes:
            raise InvalidInputError(f"Artifact of {len(data)} bytes exceeds {self._max_bytes}")
        digest = sha256_hex(data)
        uri = artifact_key(digest)
        existing = self.objects.get(uri)
        if existing is not None and sha256_hex(existing) != digest:
            raise IntegrityError(f"Stored artifact {uri} does not match its digest")
        self.objects.setdefault(uri, data)
        return ArtifactRef(
            sha256=digest,
            uri=uri,
            size_bytes=len(data),
            media_type=media_type,
            schema_name=schema_name,
        )

    async def get(self, ref: ArtifactRef) -> bytes:
        data = self.objects.get(ref.uri)
        if data is None:
            raise IntegrityError(f"Artifact {ref.uri} is missing")
        if len(data) != ref.size_bytes or sha256_hex(data) != ref.sha256:
            raise IntegrityError(f"Artifact {ref.uri} failed digest verification")
        return data

    async def put_file(
        self, path: Path, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        data = await asyncio.to_thread(path.read_bytes)
        return await self.put(data, media_type=media_type, schema_name=schema_name)

    async def download_to(self, ref: ArtifactRef, path: Path) -> None:
        await asyncio.to_thread(path.write_bytes, await self.get(ref))


@dataclass
class _Blob:
    data: bytes
    etag: str
    metadata: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    last_modified: datetime | None = None


class InMemorySourceReader:
    def __init__(
        self,
        *,
        container_name: str = "content",
        container_url: str = "https://example.blob.core.windows.net/content",
        chunk_size: int = 64 * 1024,
    ) -> None:
        self.container_name = container_name
        self.container_url = container_url
        self._chunk_size = chunk_size
        self._blobs: dict[str, _Blob] = {}
        self._revision = 0

    def put_blob(
        self,
        blob_name: str,
        data: bytes,
        *,
        declare_hash: bool = True,
        content_type: str | None = "application/pdf",
    ) -> str:
        self._revision += 1
        etag = f'"0x{self._revision:08X}"'
        metadata = {"content_hash": hashlib.sha256(data).hexdigest()} if declare_hash else {}
        self._blobs[blob_name] = _Blob(data, etag, metadata, content_type)
        return etag

    async def properties(self, blob_name: str) -> SourceProperties:
        blob = self._blobs.get(blob_name)
        if blob is None:
            raise InvalidInputError(f"Source blob not found: {blob_name}")
        return SourceProperties(
            container_name=self.container_name,
            container_url=self.container_url,
            blob_name=blob_name,
            etag=blob.etag,
            size_bytes=len(blob.data),
            content_type=blob.content_type,
            last_modified=blob.last_modified,
            declared_content_hash=blob.metadata.get("content_hash"),
        )

    async def stream(self, blob_name: str, *, etag: str) -> AsyncIterator[bytes]:
        blob = self._blobs.get(blob_name)
        if blob is None:
            raise StaleSourceError(f"Source {blob_name} was removed after {etag}")
        if blob.etag != etag:
            raise StaleSourceError(f"Source {blob_name} changed from {etag} to {blob.etag}")
        for start in range(0, len(blob.data), self._chunk_size):
            yield blob.data[start : start + self._chunk_size]
