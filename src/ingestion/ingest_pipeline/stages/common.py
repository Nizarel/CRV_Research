"""Helpers shared by the evidence stages: inputs, scratch space, sources and artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..domain.errors import (
    IntegrityError,
    InvalidInputError,
    StaleSourceError,
    UnsupportedContentError,
)
from ..domain.models import ArtifactRef, SourceSnapshot, StageJob
from .base import StageServices

M = TypeVar("M", bound=BaseModel)

JSON_MEDIA_TYPE = "application/json"
JSONL_MEDIA_TYPE = "application/x-ndjson"


def parse_inputs(model: type[M], job: StageJob) -> M:
    try:
        return model.model_validate(job.inputs)
    except ValidationError as error:
        raise InvalidInputError(
            f"Invalid {job.stage.value} inputs: {error.error_count()} error(s)"
        ) from error


@asynccontextmanager
async def scratch(services: StageServices) -> AsyncIterator[Path]:
    """A private temporary directory under the worker's bounded work dir, always removed."""

    root = services.work_dir
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="stage-", dir=root))
    try:
        yield path
    finally:
        await asyncio.to_thread(shutil.rmtree, path, True)


async def download_snapshot(
    services: StageServices, snapshot: SourceSnapshot, destination: Path, *, max_bytes: int
) -> None:
    """Stream exactly this snapshot's bytes to disk and prove they hash to its content."""

    sources = services.require_sources()
    hasher = hashlib.sha256()
    size = 0
    with destination.open("wb") as handle:
        async for chunk in sources.stream(snapshot.blob_name, etag=snapshot.etag):
            size += len(chunk)
            if size > max_bytes:
                raise UnsupportedContentError(f"Source exceeds {max_bytes} bytes")
            hasher.update(chunk)
            handle.write(chunk)
    if size != snapshot.size_bytes or hasher.hexdigest() != snapshot.content_hash:
        raise IntegrityError("Downloaded bytes do not match the recorded snapshot")


async def fetch_content(
    services: StageServices, content_hash: str, destination: Path, *, max_bytes: int
) -> SourceSnapshot:
    """Download the content from any placement that still holds it (latest first).

    Content-scoped jobs do not care which placement provides the bytes, only that they hash
    to ``content_hash``. Raises ``StaleSourceError`` when no placement holds them anymore.
    """

    candidates = await services.require_snapshots().for_content(content_hash)
    for snapshot in sorted(candidates, key=lambda s: s.observed_at, reverse=True):
        try:
            await download_snapshot(services, snapshot, destination, max_bytes=max_bytes)
        except StaleSourceError:
            continue
        return snapshot
    raise StaleSourceError(f"No placement still holds content {content_hash}")


async def put_json(services: StageServices, value: Any, *, schema_name: str) -> ArtifactRef:
    if isinstance(value, BaseModel):
        data = value.model_dump_json(by_alias=True).encode("utf-8")
    else:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return await services.require_artifacts().put(
        data, media_type=JSON_MEDIA_TYPE, schema_name=schema_name
    )


async def load_model(services: StageServices, ref: ArtifactRef, model: type[M]) -> M:
    data = await services.require_artifacts().get(ref)
    try:
        return model.model_validate_json(data)
    except ValidationError as error:
        raise IntegrityError(f"Artifact {ref.uri} does not match {model.__name__}") from error


async def put_jsonl(
    services: StageServices, items: Sequence[BaseModel], *, schema_name: str
) -> ArtifactRef:
    lines = b"".join(item.model_dump_json().encode("utf-8") + b"\n" for item in items)
    return await services.require_artifacts().put(
        lines, media_type=JSONL_MEDIA_TYPE, schema_name=schema_name
    )


async def load_jsonl(services: StageServices, ref: ArtifactRef, model: type[M]) -> list[M]:
    data = await services.require_artifacts().get(ref)
    try:
        return [model.model_validate_json(line) for line in data.splitlines() if line.strip()]
    except ValidationError as error:
        raise IntegrityError(f"Artifact {ref.uri} does not match {model.__name__}") from error


def page_ranges(pages: tuple[int, ...] | list[int]) -> str:
    """``[3, 4, 5, 7]`` -> ``"3-5,7"`` (DI's ``pages`` syntax)."""

    ranges: list[str] = []
    ordered = sorted(set(pages))
    start = previous = ordered[0] if ordered else 0
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(f"{start}-{previous}" if previous > start else str(start))
        start = previous = page
    if ordered:
        ranges.append(f"{start}-{previous}" if previous > start else str(start))
    return ",".join(ranges)
