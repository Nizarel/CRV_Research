"""Retry policy: one business retry budget per job, exponential backoff with full jitter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random

from .errors import ErrorCategory
from .models import StageOutcome, truncate_error
from .states import JobStatus

PAUSED = frozenset({ErrorCategory.AUTH, ErrorCategory.CONFIGURATION})
"""Categories that need an operator fix; retried slowly so a bad route cannot spin."""

TERMINAL_BY_CATEGORY: dict[ErrorCategory, JobStatus] = {
    ErrorCategory.INVALID_INPUT: JobStatus.QUARANTINED,
    ErrorCategory.UNSUPPORTED: JobStatus.UNSUPPORTED,
    ErrorCategory.STALE_SOURCE: JobStatus.SUPERSEDED,
    ErrorCategory.SUBMISSION_UNKNOWN: JobStatus.SUBMISSION_UNKNOWN,
}


@dataclass(frozen=True)
class RetryPolicy:
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0
    multiplier: float = 2.0
    pause_delay_seconds: float = 900.0
    max_retry_after_seconds: float = 3600.0
    throttle_seconds: float = 15.0
    """Minimum wait after throttling, plus up to the same again as jitter."""

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0 or self.multiplier < 1:
            raise ValueError("Retry base delay must be positive and multiplier >= 1")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Retry max delay must be >= base delay")

    def backoff_seconds(
        self, failure_number: int, *, retry_after: float | None, rng: Random
    ) -> float:
        """Delay before retry ``failure_number`` (1-based).

        A server ``Retry-After`` is a floor, not a suggestion: wait at least that long plus a
        small jitter so throttled callers do not return in lockstep.
        """

        if failure_number < 1:
            raise ValueError("failure_number is 1-based")
        if retry_after is not None:
            floor = min(max(retry_after, 0.0), self.max_retry_after_seconds)
            return floor + rng.uniform(0.0, self.base_delay_seconds)
        ceiling = min(
            self.max_delay_seconds,
            self.base_delay_seconds * self.multiplier ** (failure_number - 1),
        )
        return rng.uniform(0.0, ceiling)


def failure_outcome(
    *,
    category: ErrorCategory,
    message: str,
    retry_after: float | None,
    failure_count: int,
    max_failures: int,
    now: datetime,
    policy: RetryPolicy,
    rng: Random,
) -> StageOutcome:
    """Decide the outcome of a failed execution from its category and remaining budget."""

    reason = truncate_error(message)
    terminal = TERMINAL_BY_CATEGORY.get(category)
    if terminal is not None:
        return StageOutcome(status=terminal, reason=reason, error_category=category)

    if category is ErrorCategory.THROTTLED:
        # Shared quota pressure says nothing about this job: wait, never spend its budget.
        floor = min(
            max(retry_after or 0.0, policy.throttle_seconds), policy.max_retry_after_seconds
        )
        return StageOutcome(
            status=JobStatus.RETRY_WAIT,
            reason=reason,
            error_category=category,
            not_before=now
            + timedelta(seconds=floor + rng.uniform(0.0, policy.throttle_seconds)),
            retry_after_seconds=retry_after,
        )

    failure_number = failure_count + 1
    if failure_number >= max_failures:
        return StageOutcome(
            status=JobStatus.FAILED_PERMANENT,
            reason=reason,
            error_category=category,
            counts_as_failure=True,
        )

    if category in PAUSED:
        delay = max(policy.pause_delay_seconds, retry_after or 0.0)
    else:
        delay = policy.backoff_seconds(failure_number, retry_after=retry_after, rng=rng)
    return StageOutcome(
        status=JobStatus.RETRY_WAIT,
        reason=reason,
        error_category=category,
        counts_as_failure=True,
        not_before=now + timedelta(seconds=delay),
        retry_after_seconds=retry_after,
    )
