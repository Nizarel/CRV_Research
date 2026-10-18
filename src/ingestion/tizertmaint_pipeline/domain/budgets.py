"""Provider admission math: rate windows plus AIMD concurrency. State is persisted by adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .errors import ErrorCategory
from .models import StageOutcome
from .states import REMOTE_POLLS, RESCHEDULED_STATUSES, JobStatus, StageName


@dataclass(frozen=True)
class BudgetLimits:
    max_concurrency: int
    min_concurrency: int = 1
    initial_concurrency: int | None = None
    units_per_window: int | None = None
    window_seconds: float = 60.0
    default_cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.min_concurrency <= self.max_concurrency:
            raise ValueError("Require 1 <= min_concurrency <= max_concurrency")
        initial = self.initial_concurrency
        if initial is not None and not self.min_concurrency <= initial <= self.max_concurrency:
            raise ValueError("initial_concurrency must lie within [min, max]")
        if self.units_per_window is not None and self.units_per_window < 1:
            raise ValueError("units_per_window must be positive")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")


@dataclass(frozen=True)
class BudgetState:
    concurrency_limit: int
    window_started_at: datetime
    in_flight: int = 0
    used_in_window: int = 0
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class Admission:
    admitted: bool
    state: BudgetState
    retry_at: datetime | None = None
    reason: str | None = None


def initial_state(limits: BudgetLimits, now: datetime) -> BudgetState:
    return BudgetState(
        concurrency_limit=limits.initial_concurrency or limits.min_concurrency,
        window_started_at=now,
    )


def _roll_window(state: BudgetState, limits: BudgetLimits, now: datetime) -> BudgetState:
    if now >= state.window_started_at + timedelta(seconds=limits.window_seconds):
        return replace(state, window_started_at=now, used_in_window=0)
    return state


def admit(state: BudgetState, limits: BudgetLimits, *, units: int, now: datetime) -> Admission:
    """Reserve one in-flight slot and ``units`` of window quota, or say when to retry."""

    if units < 0:
        raise ValueError("units must be non-negative")
    if limits.units_per_window is not None and units > limits.units_per_window:
        raise ValueError("Request exceeds the whole window budget; split it before admission")

    state = _roll_window(state, limits, now)
    if state.cooldown_until is not None and now < state.cooldown_until:
        return Admission(False, state, retry_at=state.cooldown_until, reason="cooldown")
    if state.in_flight >= state.concurrency_limit:
        return Admission(False, state, reason="concurrency")
    if (
        limits.units_per_window is not None
        and state.used_in_window + units > limits.units_per_window
    ):
        window_end = state.window_started_at + timedelta(seconds=limits.window_seconds)
        return Admission(False, state, retry_at=window_end, reason="window")
    return Admission(
        True,
        replace(
            state,
            in_flight=state.in_flight + 1,
            used_in_window=state.used_in_window + units,
            cooldown_until=None,
        ),
    )


def release(state: BudgetState) -> BudgetState:
    return replace(state, in_flight=max(state.in_flight - 1, 0))


def freed_submit_stage(stage: StageName, outcome: StageOutcome) -> StageName | None:
    """Submit stage whose parked jobs may take the remote slot this completion frees, if any.

    A poll frees its slot once it stops rescheduling. An admitted submit frees its slot when it
    ends without a poll; budget and provider waits do not wake anyone, or one blocked submit
    would wake the whole backlog in turn.
    """

    for submit, poll in REMOTE_POLLS.items():
        if stage is poll:
            return None if outcome.status in RESCHEDULED_STATUSES else submit
    if stage in REMOTE_POLLS and outcome.status is not JobStatus.SUCCEEDED:
        quiet = {ErrorCategory.SLOT_WAIT, ErrorCategory.THROTTLED}
        return None if outcome.error_category in quiet else stage
    return None


def on_success(state: BudgetState, limits: BudgetLimits) -> BudgetState:
    """Additive increase after a successful call."""

    return replace(
        state, concurrency_limit=min(state.concurrency_limit + 1, limits.max_concurrency)
    )


def on_throttle(
    state: BudgetState, limits: BudgetLimits, *, now: datetime, retry_after: float | None
) -> BudgetState:
    """Multiplicative decrease plus a cooldown honoring the provider's Retry-After."""

    cooldown = retry_after if retry_after is not None else limits.default_cooldown_seconds
    return replace(
        state,
        concurrency_limit=max(state.concurrency_limit // 2, limits.min_concurrency),
        cooldown_until=now + timedelta(seconds=max(cooldown, 0.0)),
    )
