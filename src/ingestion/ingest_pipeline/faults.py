"""Named crash points for deterministic recovery tests. A no-op in production."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from enum import StrEnum


class FaultPoint(StrEnum):
    AFTER_CLAIM = "after_claim"
    AFTER_REGISTER = "after_register"
    AFTER_ARTIFACT_WRITE = "after_artifact_write"
    AFTER_REMOTE_ACCEPT = "after_remote_accept"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_SETTLE = "after_commit_before_settle"
    AFTER_SEND_BEFORE_MARK = "after_send_before_mark"


class SimulatedCrash(BaseException):
    """Process death stand-in. A ``BaseException`` so ordinary error handling cannot absorb it."""


class FaultInjector:
    """Raise ``SimulatedCrash`` on the n-th hit (1-based) of each armed point."""

    def __init__(self, plan: Mapping[FaultPoint, int] | None = None) -> None:
        self._plan = dict(plan or {})
        self.hits: Counter[FaultPoint] = Counter()

    def hit(self, point: FaultPoint) -> None:
        self.hits[point] += 1
        if self._plan.get(point) == self.hits[point]:
            raise SimulatedCrash(point.value)


NO_FAULTS = FaultInjector()
