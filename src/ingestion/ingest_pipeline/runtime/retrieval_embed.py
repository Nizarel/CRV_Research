"""Fill ``crv_retrieval.chunk.embedding_m3`` for a release from a local embedding model.

The text embedded is the stored ``content`` (the chunk text TE3 embedded). Vectors go through
the embedding cache first, so a rerun or another release never embeds the same text twice.
Resumable: only rows still missing a vector are read, and writes skip rows whose text changed.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.errors import InvalidInputError
from ..domain.ids import sha256_hex
from ..domain.vectors import Vector
from ..ports.clock import Clock
from ..ports.evidence import EmbeddingCache
from ..ports.providers import EmbeddingProvider

FAILED_PREVIEW = 20


class ChunkRow(Protocol):
    @property
    def projection_id(self) -> str: ...

    @property
    def content(self) -> str: ...


class VectorStore(Protocol):
    async def missing(self, release_id: str, *, after: str, limit: int) -> Sequence[ChunkRow]: ...

    async def write(self, rows: Sequence[Mapping[str, Any]]) -> int: ...

    async def status(self, release_id: str) -> dict[str, int]: ...


@dataclass(frozen=True)
class EmbedOptions:
    release_id: str
    batch_rows: int = 256
    limit: int | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.batch_rows <= 5000:
            raise InvalidInputError("batch_rows must be between 1 and 5000")
        if self.limit is not None and self.limit < 1:
            raise InvalidInputError("limit must be at least 1")


@dataclass
class EmbedReport:
    release_id: str
    model: str
    dimensions: int
    dry_run: bool
    before: dict[str, int]
    after: dict[str, int] = field(default_factory=dict)
    selected: int = 0
    cached: int = 0
    embedded: int = 0
    written: int = 0
    failed: int = 0
    failed_preview: list[str] = field(default_factory=list)
    seconds: float = 0.0


async def _embed_texts(
    provider: EmbeddingProvider, texts: Mapping[str, str]
) -> tuple[dict[str, Vector], list[str]]:
    """Vectors by text key; a rejected batch is retried item by item so one bad text (for
    example longer than the model's context) does not block the release."""

    keys = list(texts)
    if not keys:
        return {}, []
    try:
        vectors = await provider.embed([texts[key] for key in keys])
        return dict(zip(keys, vectors, strict=True)), []
    except InvalidInputError:
        if len(keys) == 1:
            return {}, keys
    found: dict[str, Vector] = {}
    rejected: list[str] = []
    for key in keys:
        try:
            [vector] = await provider.embed([texts[key]])
            found[key] = vector
        except InvalidInputError:
            rejected.append(key)
    return found, rejected


async def embed_release(
    store: VectorStore,
    provider: EmbeddingProvider,
    cache: EmbeddingCache,
    clock: Clock,
    options: EmbedOptions,
) -> EmbedReport:
    started = time.perf_counter()
    report = EmbedReport(
        release_id=options.release_id, model=provider.model, dimensions=provider.dimensions,
        dry_run=options.dry_run, before=await store.status(options.release_id),
    )
    if options.dry_run:
        report.after = report.before
        return report
    after = ""
    while options.limit is None or report.selected < options.limit:
        size = options.batch_rows
        if options.limit is not None:
            size = min(size, options.limit - report.selected)
        rows = await store.missing(options.release_id, after=after, limit=size)
        if not rows:
            break
        after = rows[-1].projection_id
        report.selected += len(rows)
        texts = {sha256_hex(row.content): row.content for row in rows}
        vectors = dict(await cache.get_many(
            list(texts), model=provider.model, dimensions=provider.dimensions
        ))
        report.cached += len(vectors)
        fresh, rejected = await _embed_texts(
            provider, {key: text for key, text in texts.items() if key not in vectors}
        )
        if fresh:
            await cache.put_many(
                fresh, model=provider.model, dimensions=provider.dimensions, now=clock.now()
            )
        report.embedded += len(fresh)
        vectors.update(fresh)
        report.written += await store.write([
            {"projection_id": row.projection_id, "content_sha256": key,
             "vector": list(vectors[key])}
            for row in rows
            if (key := sha256_hex(row.content)) in vectors
        ])
        failed = [row.projection_id for row in rows if sha256_hex(row.content) in rejected]
        report.failed += len(failed)
        report.failed_preview.extend(failed[: FAILED_PREVIEW - len(report.failed_preview)])
    report.after = await store.status(options.release_id)
    report.seconds = round(time.perf_counter() - started, 1)
    return report
