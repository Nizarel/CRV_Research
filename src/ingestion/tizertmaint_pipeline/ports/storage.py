"""Source and artifact storage ports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..domain.models import ArtifactRef, SourceProvenance


@dataclass(frozen=True)
class SourceProperties:
    """Exact properties of one source blob at the time they were read."""

    container_name: str
    container_url: str
    blob_name: str
    etag: str
    size_bytes: int
    version_id: str | None = None
    content_type: str | None = None
    last_modified: datetime | None = None
    declared_content_hash: str | None = None
    provenance: SourceProvenance | None = None


class SourceReader(Protocol):
    async def properties(self, blob_name: str) -> SourceProperties:
        """Raises ``InvalidInputError`` if the blob does not exist."""
        ...

    def stream(self, blob_name: str, *, etag: str) -> AsyncIterator[bytes]:
        """Yield the bytes of exactly ``etag``; raises ``StaleSourceError`` if it changed."""
        ...


class ArtifactStore(Protocol):
    async def put(
        self, data: bytes, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        """Store immutably by digest; an existing object must match or ``IntegrityError``."""
        ...

    async def put_file(
        self, path: Path, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        """Like ``put`` but streamed from a local file, so large artifacts are never buffered."""
        ...

    async def get(self, ref: ArtifactRef) -> bytes:
        """Return verified bytes; raises ``IntegrityError`` on digest mismatch."""
        ...

    async def download_to(self, ref: ArtifactRef, path: Path) -> None:
        """Stream to ``path`` and verify size and digest; raises ``IntegrityError``."""
        ...
