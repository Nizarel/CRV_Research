"""Classification of v1 (legacy) ingestion state; legacy rows are inventoried, never deleted."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LegacyState(StrEnum):
    UNKNOWN = "legacy_unknown"
    EVIDENCE_AVAILABLE = "legacy_evidence_available"
    VERIFIED = "verified"


@dataclass(frozen=True)
class LegacyItem:
    content_hash: str
    legacy_state: str | None
    chunks: int
    pages: int
    figures: int
    v2_evidence_id: str | None

    @property
    def legacy_key(self) -> str:
        return f"v1:{self.content_hash}"

    @property
    def state(self) -> LegacyState:
        if self.v2_evidence_id is not None:
            return LegacyState.VERIFIED
        if self.chunks > 0:
            return LegacyState.EVIDENCE_AVAILABLE
        return LegacyState.UNKNOWN

    @property
    def reason(self) -> str:
        if self.v2_evidence_id is not None:
            return "embedded v2 evidence exists for this content"
        if self.chunks > 0:
            return f"v1 holds {self.chunks} chunk(s); v2 evidence not yet built"
        return f"v1 state {self.legacy_state or 'missing'} without chunks"
