from __future__ import annotations

from datetime import UTC, datetime, timedelta
from random import Random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_pipeline.domain.budgets import (
    BudgetLimits,
    admit,
    freed_submit_stage,
    initial_state,
    on_success,
    on_throttle,
    release,
)
from ingest_pipeline.domain.errors import (
    ErrorCategory,
    InvalidInputError,
    ThrottledError,
    TransientError,
    classify,
)
from ingest_pipeline.domain.models import StageOutcome
from ingest_pipeline.domain.retry import RetryPolicy, failure_outcome
from ingest_pipeline.domain.states import JobStatus, StageName

NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = RetryPolicy(base_delay_seconds=2, max_delay_seconds=60, pause_delay_seconds=900)


@given(st.integers(min_value=1, max_value=40), st.integers(min_value=0, max_value=2**32))
def test_backoff_is_bounded_by_exponential_ceiling(failure: int, seed: int) -> None:
    delay = POLICY.backoff_seconds(failure, retry_after=None, rng=Random(seed))
    assert 0 <= delay <= min(60, 2 * 2 ** (failure - 1))


@given(st.floats(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=2**32))
def test_retry_after_is_a_floor_with_bounded_jitter(retry_after: float, seed: int) -> None:
    delay = POLICY.backoff_seconds(3, retry_after=retry_after, rng=Random(seed))
    floor = min(retry_after, POLICY.max_retry_after_seconds)
    assert floor <= delay <= floor + POLICY.base_delay_seconds


def _outcome(category: ErrorCategory, *, failures: int = 0, retry_after: float | None = None):
    return failure_outcome(
        category=category,
        message="boom",
        retry_after=retry_after,
        failure_count=failures,
        max_failures=3,
        now=NOW,
        policy=POLICY,
        rng=Random(0),
    )


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (ErrorCategory.INVALID_INPUT, JobStatus.QUARANTINED),
        (ErrorCategory.UNSUPPORTED, JobStatus.UNSUPPORTED),
        (ErrorCategory.STALE_SOURCE, JobStatus.SUPERSEDED),
        (ErrorCategory.SUBMISSION_UNKNOWN, JobStatus.SUBMISSION_UNKNOWN),
    ],
)
def test_permanent_categories_are_classified_once_without_spending_budget(
    category: ErrorCategory, status: JobStatus
) -> None:
    outcome = _outcome(category)
    assert outcome.status is status
    assert not outcome.counts_as_failure


def test_retryable_failures_back_off_until_the_budget_is_spent() -> None:
    first = _outcome(ErrorCategory.TRANSIENT, failures=0)
    assert first.status is JobStatus.RETRY_WAIT
    assert first.counts_as_failure
    assert first.not_before is not None
    assert first.not_before >= NOW

    last = _outcome(ErrorCategory.TRANSIENT, failures=2)
    assert last.status is JobStatus.FAILED_PERMANENT


def test_throttling_honours_retry_after_and_auth_pauses_the_route() -> None:
    throttled = _outcome(ErrorCategory.THROTTLED, retry_after=30)
    assert throttled.not_before is not None
    assert throttled.not_before >= NOW + timedelta(seconds=30)

    exhausted = _outcome(ErrorCategory.THROTTLED, failures=2)
    assert exhausted.status is JobStatus.RETRY_WAIT
    assert not exhausted.counts_as_failure
    assert exhausted.not_before is not None
    assert exhausted.not_before >= NOW + timedelta(seconds=POLICY.throttle_seconds)

    paused = _outcome(ErrorCategory.AUTH)
    assert paused.not_before == NOW + timedelta(seconds=900)


def test_classify_maps_exceptions_to_categories() -> None:
    assert classify(ThrottledError("x", retry_after=4)) == (ErrorCategory.THROTTLED, 4)
    assert classify(InvalidInputError("x"))[0] is ErrorCategory.INVALID_INPUT
    assert classify(TimeoutError())[0] is ErrorCategory.TIMEOUT
    assert classify(ConnectionResetError())[0] is ErrorCategory.TRANSIENT
    assert classify(TransientError("x"))[0] is ErrorCategory.TRANSIENT
    assert classify(KeyError("x"))[0] is ErrorCategory.BUG


def test_classify_retries_postgres_deadlocks_and_serialization_failures() -> None:
    class _PostgresError(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    assert classify(_PostgresError("40P01"))[0] is ErrorCategory.TRANSIENT
    assert classify(_PostgresError("40001"))[0] is ErrorCategory.TRANSIENT
    assert classify(_PostgresError("57014"))[0] is ErrorCategory.TRANSIENT
    assert classify(_PostgresError("23505"))[0] is ErrorCategory.BUG


def test_only_slot_releases_wake_a_parked_submit() -> None:
    # Rollout 2026-10-13: thousands of blocked submits retrying on timers loaded PostgreSQL to 92%
    # CPU; parked submits are woken when a slot frees instead.
    done = StageOutcome.succeeded()
    polling = StageOutcome(status=JobStatus.AWAITING_REMOTE, not_before=NOW)
    failed = StageOutcome(status=JobStatus.FAILED_PERMANENT, reason="bad page")

    def waiting(category: ErrorCategory) -> StageOutcome:
        return StageOutcome(status=JobStatus.RETRY_WAIT, not_before=NOW, error_category=category)

    assert freed_submit_stage(StageName.CU_POLL, done) is StageName.CU_SUBMIT
    assert freed_submit_stage(StageName.DI_POLL, failed) is StageName.DI_SUBMIT
    assert freed_submit_stage(StageName.CU_POLL, polling) is None
    assert freed_submit_stage(StageName.CU_SUBMIT, done) is None
    assert freed_submit_stage(StageName.CU_SUBMIT, failed) is StageName.CU_SUBMIT
    assert freed_submit_stage(StageName.CU_SUBMIT, waiting(ErrorCategory.TRANSIENT)) is (
        StageName.CU_SUBMIT
    )
    assert freed_submit_stage(StageName.CU_SUBMIT, waiting(ErrorCategory.SLOT_WAIT)) is None
    assert freed_submit_stage(StageName.DI_SUBMIT, waiting(ErrorCategory.THROTTLED)) is None
    assert freed_submit_stage(StageName.CHUNK, done) is None


def test_budget_admits_within_concurrency_and_window() -> None:
    limits = BudgetLimits(max_concurrency=4, initial_concurrency=2, units_per_window=10)
    state = initial_state(limits, NOW)

    first = admit(state, limits, units=4, now=NOW)
    second = admit(first.state, limits, units=4, now=NOW)
    assert first.admitted
    assert second.admitted
    blocked = admit(second.state, limits, units=1, now=NOW)
    assert (blocked.admitted, blocked.reason) == (False, "concurrency")

    freed = release(second.state)
    over_window = admit(freed, limits, units=3, now=NOW)
    assert (over_window.admitted, over_window.reason) == (False, "window")
    assert over_window.retry_at == NOW + timedelta(seconds=60)

    next_window = admit(freed, limits, units=3, now=NOW + timedelta(seconds=61))
    assert next_window.admitted


def test_budget_throttle_halves_concurrency_and_cools_down() -> None:
    limits = BudgetLimits(max_concurrency=8, initial_concurrency=8)
    state = on_throttle(initial_state(limits, NOW), limits, now=NOW, retry_after=20)
    assert state.concurrency_limit == 4
    cooling = admit(state, limits, units=1, now=NOW + timedelta(seconds=5))
    assert (cooling.admitted, cooling.reason) == (False, "cooldown")
    assert admit(state, limits, units=1, now=NOW + timedelta(seconds=21)).admitted

    assert on_success(state, limits).concurrency_limit == 5
    floor = on_throttle(
        on_throttle(
            on_throttle(state, limits, now=NOW, retry_after=None), limits, now=NOW, retry_after=None
        ),
        limits,
        now=NOW,
        retry_after=None,
    )
    assert floor.concurrency_limit == limits.min_concurrency


def test_budget_rejects_unsatisfiable_requests() -> None:
    limits = BudgetLimits(max_concurrency=1, units_per_window=5)
    with pytest.raises(ValueError, match="split"):
        admit(initial_state(limits, NOW), limits, units=6, now=NOW)
    with pytest.raises(ValueError, match="min_concurrency"):
        BudgetLimits(max_concurrency=1, min_concurrency=2)
