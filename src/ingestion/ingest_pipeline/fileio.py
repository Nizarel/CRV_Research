"""Small streaming file helpers shared by stages and adapters (stdlib only)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

CHUNK_BYTES = 1024 * 1024


def iter_file(path: Path, *, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            yield chunk


def sha256_file(path: Path) -> tuple[str, int]:
    """Digest and size of a file, read in bounded chunks."""

    hasher = hashlib.sha256()
    size = 0
    for chunk in iter_file(path):
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size
