"""Snapshot stage: pin one exact source version, verify its bytes and record it immutably.

A job is pinned to the ETag (and optionally the hash) seen when it was planned. If the
source changed, the job is superseded and a successor is planned for what is there now,
so new bytes are never processed under an old snapshot (ING-A11).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from ..domain.errors import (
    ConfigurationError,
    IntegrityError,
    InvalidInputError,
    StaleSourceError,
    UnsupportedContentError,
)
from ..domain.ids import (
    Sha256Hex,
    legacy_placement_id,
    legacy_source_object_id,
    snapshot_id_for,
)
from ..domain.models import JobSpec, SourceSnapshot, StageJob, StageOutcome
from ..domain.planning import JobPlanner
from ..domain.states import StageName
from ..faults import FaultPoint
from ..ports.storage import SourceProperties, SourceReader
from .base import StageContext

STAGE_VERSION = "2"

SuccessorFactory = Callable[[SourceSnapshot, JobPlanner], Sequence[JobSpec]]


class SnapshotInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    container_name: str
    container_url: str
    blob_name: str
    expected_etag: str
    expected_content_hash: Sha256Hex | None = None


def snapshot_job(
    planner: JobPlanner,
    *,
    container_name: str,
    container_url: str,
    blob_name: str,
    etag: str,
    content_hash: str | None = None,
    priority: int = 100,
) -> JobSpec:
    """Plan a snapshot of ``blob_name`` as listed with ``etag``; re-planning is idempotent."""

    inputs = SnapshotInputs(
        container_name=container_name,
        container_url=container_url.rstrip("/"),
        blob_name=blob_name,
        expected_etag=etag,
        expected_content_hash=content_hash,
    )
    return planner.spec(
        StageName.SNAPSHOT,
        unit_id=legacy_source_object_id(inputs.container_url, blob_name),
        inputs=inputs.model_dump(exclude_none=True),
        content_hash=content_hash,
        priority=priority,
    )


class SnapshotStage:
    def __init__(
        self, *, max_source_bytes: int, successors: SuccessorFactory | None = None
    ) -> None:
        self._max_source_bytes = max_source_bytes
        self._successors = successors

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        try:
            inputs = SnapshotInputs.model_validate(job.inputs)
        except ValidationError as error:
            raise InvalidInputError(
                f"Invalid snapshot inputs: {error.error_count()} error(s)"
            ) from error
        services = context.services
        sources = services.require_sources()

        properties = await sources.properties(inputs.blob_name)
        if (
            properties.container_name != inputs.container_name
            or properties.container_url.rstrip("/") != inputs.container_url
        ):
            raise ConfigurationError("Worker source container differs from the job's container")
        if properties.etag != inputs.expected_etag:
            return self._supersede(
                services.planner, inputs, properties.etag, None, reason="source ETag changed"
            )
        if properties.size_bytes > self._max_source_bytes:
            raise UnsupportedContentError(
                f"Source has {properties.size_bytes} bytes; limit is {self._max_source_bytes}"
            )

        try:
            digest, size = await self._hash(sources, inputs.blob_name, properties.etag)
        except StaleSourceError:
            return await self._after_concurrent_change(sources, services.planner, inputs)
        if size != properties.size_bytes:
            raise IntegrityError(
                f"Read {size} bytes but properties declared {properties.size_bytes}"
            )
        if inputs.expected_content_hash is not None and inputs.expected_content_hash != digest:
            return self._supersede(
                services.planner,
                inputs,
                properties.etag,
                digest,
                reason="source bytes differ from the planned content hash",
            )

        snapshot = self._snapshot(inputs, properties, digest, context)
        stored = await services.require_snapshots().record(snapshot)
        services.faults.hit(FaultPoint.AFTER_REGISTER)

        successors = tuple(self._successors(stored, services.planner)) if self._successors else ()
        return StageOutcome.succeeded(
            checkpoint={
                "snapshot_id": stored.snapshot_id,
                "content_hash": digest,
                "declared_hash_matches": properties.declared_content_hash in (None, digest),
            },
            next_jobs=successors,
        )

    async def _hash(self, sources: SourceReader, blob_name: str, etag: str) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        async for chunk in sources.stream(blob_name, etag=etag):
            size += len(chunk)
            if size > self._max_source_bytes:
                raise UnsupportedContentError(f"Source exceeds {self._max_source_bytes} bytes")
            hasher.update(chunk)
        return hasher.hexdigest(), size

    async def _after_concurrent_change(
        self, sources: SourceReader, planner: JobPlanner, inputs: SnapshotInputs
    ) -> StageOutcome:
        try:
            current = await sources.properties(inputs.blob_name)
        except InvalidInputError:
            return StageOutcome.superseded("source was removed while it was being read")
        return self._supersede(
            planner, inputs, current.etag, None, reason="source changed while it was being read"
        )

    @staticmethod
    def _supersede(
        planner: JobPlanner,
        inputs: SnapshotInputs,
        etag: str,
        content_hash: str | None,
        *,
        reason: str,
    ) -> StageOutcome:
        successor = snapshot_job(
            planner,
            container_name=inputs.container_name,
            container_url=inputs.container_url,
            blob_name=inputs.blob_name,
            etag=etag,
            content_hash=content_hash,
        )
        return StageOutcome.superseded(reason, successors=(successor,))

    @staticmethod
    def _snapshot(
        inputs: SnapshotInputs, properties: SourceProperties, digest: str, context: StageContext
    ) -> SourceSnapshot:
        source_object_id = legacy_source_object_id(inputs.container_url, inputs.blob_name)
        return SourceSnapshot(
            snapshot_id=snapshot_id_for(source_object_id, properties.etag, digest),
            source_object_id=source_object_id,
            placement_id=legacy_placement_id(source_object_id, inputs.blob_name),
            container_name=inputs.container_name,
            blob_name=inputs.blob_name,
            etag=properties.etag,
            version_id=properties.version_id,
            content_hash=digest,
            size_bytes=properties.size_bytes,
            content_type=properties.content_type,
            last_modified=properties.last_modified,
            observed_at=context.services.clock.now(),
            provenance=properties.provenance,
        )
