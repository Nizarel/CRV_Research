import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from crv_agent.adapters.blob import DIGEST
from crv_agent.ports.sources import SourceInfo, SourceNotFound

CHUNK = 256 * 1024


class FilesystemSources:
    """Source PDFs as ``<root>/crv/sources/<sha256>.pdf``, the Blob layout, on a local volume."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def _path(self, digest: str) -> Path:
        if not DIGEST.fullmatch(digest):
            raise SourceNotFound(digest)
        path = (self.root / "crv" / "sources" / f"{digest}.pdf").resolve()
        # A symlink may not lead outside the source root.
        if not path.is_relative_to(self.root) or not path.is_file():
            raise SourceNotFound(digest)
        return path

    async def stat(self, digest: str) -> SourceInfo:
        path = await asyncio.to_thread(self._path, digest)
        size = (await asyncio.to_thread(path.stat)).st_size
        # Content-addressed: the digest and size identify the bytes, whatever the mtime.
        return SourceInfo(size=size, etag=f"{digest}-{size:x}")

    async def read(self, digest: str, start: int, end: int) -> AsyncIterator[bytes]:
        path = await asyncio.to_thread(self._path, digest)
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            await asyncio.to_thread(handle.seek, start)
            remaining = end - start
            while remaining > 0:
                chunk = await asyncio.to_thread(handle.read, min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)
