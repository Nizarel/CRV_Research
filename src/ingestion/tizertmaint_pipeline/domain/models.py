"""Immutable pipeline records shared by ports, adapters, stages and the runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .envelope import WorkEnvelope
from .errors import ErrorCategory
from .ids import Sha256Hex, fingerprint, job_id_for
from .states import (
    ACTIVE_STATUSES,
    OUTCOME_STATUSES,
    RESCHEDULED_STATUSES,
    JobStatus,
    StageName,
    WorkerPool,
)

MAX_ERROR_LENGTH = 2000


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceProvenance(Frozen):
    source_path: str
    original_hash: Sha256Hex
    original_blob: str
    archive_members: tuple[str, ...] = ()
    converter: str | None = None
    manifest_blob: str


class SourceSnapshot(Frozen):
    """One immutable observation of source bytes at a Blob path."""

    snapshot_id: str
    source_object_id: str
    placement_id: str
    container_name: str
    blob_name: str
    etag: str = Field(min_length=1)
    version_id: str | None = None
    content_hash: Sha256Hex
    size_bytes: int = Field(ge=0)
    content_type: str | None = None
    last_modified: AwareDatetime | None = None
    observed_at: AwareDatetime
    provenance: SourceProvenance | None = None


class ArtifactRef(Frozen):
    """Content-addressed artifact; ``uri`` is relative to the artifact store."""

    sha256: Sha256Hex
    uri: str
    size_bytes: int = Field(ge=0)
    media_type: str
    schema_name: str | None = None


class ArtifactLink(Frozen):
    role: str = Field(min_length=1, max_length=64)
    artifact: ArtifactRef


class OperationRef(Frozen):
    """Durable handle of an accepted remote operation (for example an Operation-Location)."""

    provider: str
    operation_url: str
    api_version: str
    submitted_at: AwareDatetime
    request_id: str | None = None


class ProviderUsage(Frozen):
    provider: str
    pages: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)
    account: str | None = None
    """Billing account that ran the operation; ``None`` is this subscription's resources."""
    cost_usd: float | None = Field(default=None, ge=0)
    """List-price cost when prices are configured."""


class UnitDisposition(StrEnum):
    """Final state of one extraction unit; ``None`` on the unit row means still open."""

    EVIDENCE = "evidence"
    NO_EVIDENCE = "no_evidence"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


UNIT_DISPOSITION_BY_STATUS: dict[JobStatus, UnitDisposition] = {
    JobStatus.NO_EVIDENCE: UnitDisposition.NO_EVIDENCE,
    JobStatus.FAILED_PERMANENT: UnitDisposition.FAILED,
    JobStatus.UNSUPPORTED: UnitDisposition.UNSUPPORTED,
    JobStatus.QUARANTINED: UnitDisposition.QUARANTINED,
    JobStatus.SUPERSEDED: UnitDisposition.SUPERSEDED,
    JobStatus.CANCELLED: UnitDisposition.CANCELLED,
}
"""Terminal job statuses that settle the job's unit even without an explicit result."""


class UnitRecord(Frozen):
    """One bounded extraction unit of a plan, mapped to immutable original locations."""

    unit_id: str = Field(min_length=1, max_length=512)
    ordinal: int = Field(ge=0)
    kind: str
    route: str
    pages: tuple[int, ...] = ()
    locator: dict[str, Any] = Field(default_factory=dict)
    signals: dict[str, Any] = Field(default_factory=dict)
    disposition: UnitDisposition | None = None
    reason: str | None = Field(default=None, max_length=MAX_ERROR_LENGTH)

    @model_validator(mode="after")
    def _pages_are_original_indices(self) -> Self:
        if any(page < 1 for page in self.pages) or list(self.pages) != sorted(set(self.pages)):
            raise ValueError("Unit pages must be sorted, unique, 1-based original page numbers")
        return self


class PlanRecord(Frozen):
    """Content-scoped extraction plan; registered atomically with its unit jobs."""

    plan_id: str = Field(min_length=1, max_length=512)
    content_hash: Sha256Hex
    snapshot_id: str | None = None
    policy_version: str
    family: str
    preflight_sha256: Sha256Hex | None = None
    page_count: int | None = Field(default=None, ge=0)
    units: tuple[UnitRecord, ...]

    @model_validator(mode="after")
    def _unique_units(self) -> Self:
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("Plan unit IDs must be unique")
        if sorted(unit.ordinal for unit in self.units) != list(range(len(self.units))):
            raise ValueError("Plan unit ordinals must be 0..n-1")
        return self


class UnitResult(Frozen):
    """Explicit final result reported by a unit's last stage."""

    disposition: UnitDisposition
    evidence: ArtifactRef | None = None
    reason: str | None = Field(default=None, max_length=MAX_ERROR_LENGTH)

    @model_validator(mode="after")
    def _evidence_matches(self) -> Self:
        if (self.disposition is UnitDisposition.EVIDENCE) != (self.evidence is not None):
            raise ValueError("Only an 'evidence' result carries (and requires) an artifact")
        return self


class UnitState(Frozen):
    unit: UnitRecord
    plan_id: str
    evidence: ArtifactRef | None = None
    disposed_at: AwareDatetime | None = None

    @property
    def disposition(self) -> UnitDisposition | None:
        return self.unit.disposition


class JobSpec(Frozen):
    """Request to create a stage job; identity is ``(unit_id, stage, fingerprint)``."""

    unit_id: str = Field(min_length=1, max_length=512)
    stage: StageName
    pool: WorkerPool
    fingerprint: Sha256Hex
    inputs: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    source_snapshot_id: str | None = None
    content_hash: Sha256Hex | None = None
    max_failures: int = Field(default=5, ge=1)
    not_before: AwareDatetime | None = None
    barrier_plan_id: str | None = None
    """Create the job ``blocked`` until every unit of this plan has a disposition."""

    @property
    def job_id(self) -> str:
        return job_id_for(self.unit_id, self.stage.value, self.fingerprint)

    @classmethod
    def create(
        cls,
        *,
        stage: StageName,
        pool: WorkerPool,
        unit_id: str,
        stage_version: str,
        inputs: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Self:
        payload = {"unit_id": unit_id, "inputs": inputs or {}, "stage_version": stage_version}
        return cls(
            stage=stage,
            pool=pool,
            unit_id=unit_id,
            fingerprint=fingerprint(f"stage:{stage.value}", payload),
            inputs=inputs or {},
            **fields,
        )


class StageJob(Frozen):
    job_id: str
    unit_id: str
    stage: StageName
    pool: WorkerPool
    fingerprint: Sha256Hex
    status: JobStatus
    priority: int
    inputs: dict[str, Any]
    checkpoint: dict[str, Any]
    source_snapshot_id: str | None
    content_hash: Sha256Hex | None
    parent_job_id: str | None
    not_before: AwareDatetime
    attempt: int
    failure_count: int
    max_failures: int
    fencing_token: int
    lease_owner: str | None
    lease_expires_at: AwareDatetime | None
    current_run_id: str | None
    last_error_category: ErrorCategory | None
    last_error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    def envelope(self, event_id: str, traceparent: str | None = None) -> WorkEnvelope:
        return WorkEnvelope(
            event_id=event_id,
            job_id=self.job_id,
            stage=self.stage,
            unit_id=self.unit_id,
            fingerprint=self.fingerprint,
            source_snapshot_id=self.source_snapshot_id,
            content_hash=self.content_hash,
            traceparent=traceparent,
        )


class Lease(Frozen):
    """Exclusive, fenced right to execute one attempt of a job."""

    job: StageJob
    run_id: str

    @model_validator(mode="after")
    def _active(self) -> Self:
        if self.job.status not in ACTIVE_STATUSES or self.job.lease_expires_at is None:
            raise ValueError("A lease requires an active job with an expiry")
        return self

    @property
    def fencing_token(self) -> int:
        return self.job.fencing_token

    @property
    def expires_at(self) -> AwareDatetime:
        if self.job.lease_expires_at is None:
            raise RuntimeError("Lease invariant violated: missing expiry")
        return self.job.lease_expires_at


class StageOutcome(Frozen):
    """Result of one execution, committed atomically with artifacts and successor jobs."""

    status: JobStatus
    reason: str | None = Field(default=None, max_length=MAX_ERROR_LENGTH)
    error_category: ErrorCategory | None = None
    counts_as_failure: bool = False
    not_before: AwareDatetime | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    checkpoint: dict[str, Any] | None = None
    artifacts: tuple[ArtifactLink, ...] = ()
    next_jobs: tuple[JobSpec, ...] = ()
    operation: OperationRef | None = None
    usage: ProviderUsage | None = None
    plan: PlanRecord | None = None
    unit_result: UnitResult | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"{self.status.value} is not a stage outcome status")
        if self.status in RESCHEDULED_STATUSES and self.not_before is None:
            raise ValueError(f"{self.status.value} requires not_before")
        if self.next_jobs and self.status not in {
            JobStatus.SUCCEEDED,
            JobStatus.SUPERSEDED,
            JobStatus.NO_EVIDENCE,
            JobStatus.NEEDS_REVIEW,
        }:
            raise ValueError("Only completed outcomes may create successor jobs")
        if self.plan is not None and self.status is not JobStatus.SUCCEEDED:
            raise ValueError("Only a succeeded outcome may register a plan")
        if self.unit_result is not None and self.status not in {
            JobStatus.SUCCEEDED,
            JobStatus.NO_EVIDENCE,
        }:
            raise ValueError("A unit result requires a succeeded or no_evidence outcome")
        return self

    def settles_unit(self) -> tuple[UnitDisposition, str | None, ArtifactRef | None] | None:
        """Disposition this outcome gives to the job's unit, if the unit is a plan unit."""

        if self.unit_result is not None:
            result = self.unit_result
            return result.disposition, result.reason, result.evidence
        disposition = UNIT_DISPOSITION_BY_STATUS.get(self.status)
        return None if disposition is None else (disposition, self.reason, None)

    @classmethod
    def succeeded(
        cls,
        *,
        artifacts: tuple[ArtifactLink, ...] = (),
        next_jobs: tuple[JobSpec, ...] = (),
        checkpoint: dict[str, Any] | None = None,
        usage: ProviderUsage | None = None,
        plan: PlanRecord | None = None,
        unit_result: UnitResult | None = None,
    ) -> Self:
        return cls(
            status=JobStatus.SUCCEEDED,
            artifacts=artifacts,
            next_jobs=next_jobs,
            checkpoint=checkpoint,
            usage=usage,
            plan=plan,
            unit_result=unit_result,
        )

    @classmethod
    def awaiting_remote(
        cls,
        *,
        next_poll_at: AwareDatetime,
        operation: OperationRef,
        checkpoint: dict[str, Any] | None = None,
    ) -> Self:
        return cls(
            status=JobStatus.AWAITING_REMOTE,
            not_before=next_poll_at,
            operation=operation,
            checkpoint=checkpoint,
        )

    @classmethod
    def superseded(cls, reason: str, *, successors: tuple[JobSpec, ...] = ()) -> Self:
        return cls(
            status=JobStatus.SUPERSEDED,
            reason=reason,
            error_category=ErrorCategory.STALE_SOURCE,
            next_jobs=successors,
        )


class OutboxEvent(Frozen):
    event_id: str
    job_id: str
    pool: WorkerPool
    envelope: WorkEnvelope
    available_at: AwareDatetime


class AttemptRecord(Frozen):
    run_id: str
    job_id: str
    attempt: int
    fencing_token: int
    worker: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: JobStatus | None = None
    error_category: ErrorCategory | None = None
    error: str | None = None
    operation: OperationRef | None = None
    usage: ProviderUsage | None = None


def truncate_error(message: str) -> str:
    return message if len(message) <= MAX_ERROR_LENGTH else message[: MAX_ERROR_LENGTH - 1] + "…"
