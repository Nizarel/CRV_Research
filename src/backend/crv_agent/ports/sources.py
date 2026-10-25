from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


class SourceNotFound(LookupError):
    pass


@dataclass(frozen=True)
class SourceInfo:
    size: int
    etag: str


class Sources(Protocol):
    """Read-only access to the content-addressed source PDFs that evidence pages refer to."""

    async def stat(self, digest: str) -> SourceInfo: ...

    def read(self, digest: str, start: int, end: int) -> AsyncIterator[bytes]:
        """Bytes ``start`` (inclusive) to ``end`` (exclusive)."""
        ...
