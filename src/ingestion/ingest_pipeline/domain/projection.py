"""Per-item classification of projection (search indexing) results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ACCEPTED_STATUS_CODES = frozenset({200, 201})
RETRYABLE_STATUS_CODES = frozenset({409, 422, 503})
"""Azure AI Search per-document codes worth retrying (conflict, transient, unavailable)."""


class ItemDisposition(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


def classify_item(*, succeeded: bool, status_code: int) -> ItemDisposition:
    if succeeded and status_code in ACCEPTED_STATUS_CODES:
        return ItemDisposition.ACCEPTED
    if status_code in RETRYABLE_STATUS_CODES:
        return ItemDisposition.RETRYABLE
    return ItemDisposition.PERMANENT


@dataclass(frozen=True)
class ProjectionReport:
    """Outcome per key; HTTP-level success alone never makes a projection complete."""

    accepted: tuple[str, ...] = ()
    retryable: Mapping[str, str] = field(default_factory=dict)
    permanent: Mapping[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.retryable and not self.permanent

    def merge(self, other: ProjectionReport) -> ProjectionReport:
        accepted = dict.fromkeys((*self.accepted, *other.accepted))
        retryable = {**self.retryable, **other.retryable}
        permanent = {**self.permanent, **other.permanent}
        for key in accepted:
            retryable.pop(key, None)
            permanent.pop(key, None)
        return ProjectionReport(tuple(accepted), retryable, permanent)


class ProjectionState(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    VERIFIED = "verified"
    RETIRED = "retired"
    FAILED_PERMANENT = "failed_permanent"


LIVE_STATES = frozenset({ProjectionState.ACKNOWLEDGED, ProjectionState.VERIFIED})
"""States in which a row may exist in the target store."""


class ProjectionItem(BaseModel):
    """Expected state of one projected row; the barrier trusts this, not request success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store: str
    target: str
    projection_id: str
    release_id: str
    evidence_id: str
    chunk_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    placement_id: str
    """The canonical placement that names the row; the row lists every current placement."""
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ProjectionState = ProjectionState.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
