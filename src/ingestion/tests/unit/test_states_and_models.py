from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tizertmaint_pipeline.domain.envelope import MAX_ENVELOPE_BYTES, WorkEnvelope
from tizertmaint_pipeline.domain.errors import InvalidInputError
from tizertmaint_pipeline.domain.models import JobSpec, StageOutcome
from tizertmaint_pipeline.domain.states import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    CLAIMABLE_STATUSES,
    OUTCOME_STATUSES,
    TERMINAL_STATUSES,
    JobStatus,
    StageName,
    WorkerPool,
    can_transition,
    require_transition,
)

DIGEST = "c" * 64


def test_every_status_has_a_transition_entry_and_sets_are_disjoint() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)
    assert not CLAIMABLE_STATUSES & ACTIVE_STATUSES
    assert not CLAIMABLE_STATUSES & TERMINAL_STATUSES
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES


@pytest.mark.parametrize("active", sorted(ACTIVE_STATUSES))
def test_active_jobs_can_report_every_outcome(active: JobStatus) -> None:
    assert all(can_transition(active, outcome) for outcome in OUTCOME_STATUSES)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_jobs_cannot_be_leased_again(terminal: JobStatus) -> None:
    assert not can_transition(terminal, JobStatus.LEASED)


def test_require_transition_rejects_illegal_moves() -> None:
    require_transition(JobStatus.PENDING, JobStatus.LEASED)
    with pytest.raises(ValueError, match="succeeded -> leased"):
        require_transition(JobStatus.SUCCEEDED, JobStatus.LEASED)


def _envelope(**overrides: object) -> WorkEnvelope:
    fields: dict[str, object] = {
        "event_id": "event-1",
        "job_id": "job-1",
        "stage": StageName.SNAPSHOT,
        "unit_id": "unit-1",
        "fingerprint": DIGEST,
    }
    fields.update(overrides)
    return WorkEnvelope.model_validate(fields)


def test_envelope_round_trips_without_content() -> None:
    envelope = _envelope(content_hash=DIGEST)
    assert WorkEnvelope.parse(envelope.to_bytes()) == envelope
    assert b'"schema_version":"2"' in envelope.to_bytes()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"schema_version":"1","event_id":"e","job_id":"j","stage":"snapshot",'
        b'"unit_id":"u","fingerprint":"' + DIGEST.encode() + b'"}',
        _envelope().to_bytes().replace(b"}", b',"text":"leaked"}'),
        b'{"event_id":"e"}',
        b" " * (MAX_ENVELOPE_BYTES + 1),
    ],
)
def test_envelope_rejects_malformed_or_oversized_bodies(body: bytes) -> None:
    with pytest.raises(InvalidInputError):
        WorkEnvelope.parse(body)


def test_job_spec_identity_follows_inputs_and_stage_version() -> None:
    def spec(inputs: dict[str, object], version: str = "1") -> JobSpec:
        return JobSpec.create(
            stage=StageName.SNAPSHOT,
            pool=WorkerPool.IO,
            unit_id="unit",
            stage_version=version,
            inputs=inputs,
        )

    assert spec({"a": 1}).job_id == spec({"a": 1}).job_id
    assert spec({"a": 1}).job_id != spec({"a": 2}).job_id
    assert spec({"a": 1}).job_id != spec({"a": 1}, version="2").job_id


def test_outcome_invariants() -> None:
    later = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=5)
    with pytest.raises(ValidationError, match="requires not_before"):
        StageOutcome(status=JobStatus.RETRY_WAIT)
    with pytest.raises(ValidationError, match="not a stage outcome"):
        StageOutcome(status=JobStatus.LEASED)
    successor = JobSpec.create(
        stage=StageName.SNAPSHOT, pool=WorkerPool.IO, unit_id="u", stage_version="1"
    )
    with pytest.raises(ValidationError, match="successor"):
        StageOutcome(status=JobStatus.RETRY_WAIT, not_before=later, next_jobs=(successor,))
    assert StageOutcome.succeeded(next_jobs=(successor,)).next_jobs == (successor,)
