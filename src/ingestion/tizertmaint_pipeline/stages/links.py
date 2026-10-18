"""Job builders that link the evidence stages, and the snapshot successor (placement router).

Extraction jobs are keyed by content, so every placement of the same bytes lands on the same
preflight/plan/extraction jobs (one paid analysis). Projection is keyed by content too: one
row per chunk lists every current placement of the bytes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..domain.ids import Sha256Hex
from ..domain.models import ArtifactRef, JobSpec, SourceSnapshot, UnitRecord
from ..domain.planning import JobPlanner
from ..domain.routing import RoutingPolicy
from ..domain.states import StageName


class _Inputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PreflightInputs(_Inputs):
    content_hash: Sha256Hex
    policy: RoutingPolicy


class PlanInputs(_Inputs):
    preflight: ArtifactRef
    policy: RoutingPolicy


class UnitInputs(_Inputs):
    """Inputs shared by every unit-scoped extraction stage."""

    plan_id: str
    content_hash: Sha256Hex
    family: str
    file_kind: str
    unit: UnitRecord


class ProjectInputs(_Inputs):
    content_hash: Sha256Hex
    policy_version: str
    evidence_id: str | None = None
    """Unset when planned by a new placement: project whatever evidence is current."""
    trigger_snapshot_id: str | None = None
    """The snapshot that changed this content's placements; each change is a new job."""


def content_unit(content_hash: str) -> str:
    return f"content:{content_hash}"


def preflight_job(planner: JobPlanner, content_hash: str, policy: RoutingPolicy) -> JobSpec:
    inputs = PreflightInputs(content_hash=content_hash, policy=policy)
    return planner.spec(
        StageName.PREFLIGHT,
        unit_id=content_unit(content_hash),
        inputs=inputs.model_dump(mode="json"),
        content_hash=content_hash,
    )


def project_job(
    planner: JobPlanner,
    *,
    content_hash: str,
    policy_version: str,
    evidence_id: str | None = None,
    trigger_snapshot_id: str | None = None,
) -> JobSpec:
    inputs = ProjectInputs(
        content_hash=content_hash,
        policy_version=policy_version,
        evidence_id=evidence_id,
        trigger_snapshot_id=trigger_snapshot_id,
    )
    return planner.spec(
        StageName.PROJECT_SEARCH,
        unit_id=f"project:{content_hash}",
        inputs=inputs.model_dump(mode="json", exclude_none=True),
        content_hash=content_hash,
    )


def unit_job(planner: JobPlanner, stage: StageName, inputs: UnitInputs, **fields: Any) -> JobSpec:
    return planner.spec(
        stage,
        unit_id=inputs.unit.unit_id,
        inputs=inputs.model_dump(mode="json", exclude_none=True),
        content_hash=inputs.content_hash,
        **fields,
    )


class PlacementRouter:
    """Snapshot successor: plan content once, and refresh the content's rows for this placement."""

    def __init__(self, policy: RoutingPolicy) -> None:
        self._policy = policy

    def __call__(self, snapshot: SourceSnapshot, planner: JobPlanner) -> list[JobSpec]:
        return [
            preflight_job(planner, snapshot.content_hash, self._policy),
            project_job(
                planner,
                content_hash=snapshot.content_hash,
                policy_version=self._policy.version,
                trigger_snapshot_id=snapshot.snapshot_id,
            ),
        ]
