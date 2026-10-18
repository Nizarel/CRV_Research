"""Operator replay contract shared by the in-memory and PostgreSQL ledgers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pipeline_harness import START

from tizertmaint_pipeline.adapters.memory.ledger import InMemoryLedger
from tizertmaint_pipeline.domain.errors import InvalidInputError
from tizertmaint_pipeline.domain.models import JobSpec, PlanRecord, StageOutcome, UnitRecord
from tizertmaint_pipeline.domain.states import JobStatus, StageName, WorkerPool

pytestmark = pytest.mark.contract


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def ledger(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryLedger()
    from tizertmaint_pipeline.adapters.postgres.ledger import PostgresLedger

    return PostgresLedger(request.getfixturevalue("pg_pool"))


async def _finish(ledger: Any, unit: str, status: JobStatus) -> str:
    spec = JobSpec.create(
        stage=StageName.DI_SUBMIT,
        pool=WorkerPool.IO,
        unit_id=unit,
        stage_version="1",
        inputs={"unit": unit},
    )
    await ledger.enqueue([spec], now=START)
    lease = await ledger.claim(spec.job_id, owner="w", lease_seconds=60, now=START)
    assert lease is not None
    await ledger.complete(lease, StageOutcome(status=status, reason="stuck"), now=START)
    return spec.job_id


@pytest.mark.parametrize(
    ("status", "resumed"),
    [
        (JobStatus.SUBMISSION_UNKNOWN, JobStatus.RETRY_WAIT),
        (JobStatus.NEEDS_REVIEW, JobStatus.PENDING),
    ],
)
async def test_replay_resumes_and_is_claimable(ledger: Any, status, resumed) -> None:
    job_id = await _finish(ledger, f"unit-{status.value}", status)
    later = START + timedelta(minutes=5)
    job = await ledger.replay(job_id, actor="ops@example", reason="checked portal", now=later)
    assert job.status is resumed
    assert "ops@example" in (job.last_error or "")
    assert await ledger.claim(job_id, owner="w2", lease_seconds=60, now=later) is not None
    events = await ledger.lease_batch(now=later, limit=10, claim_seconds=30)
    assert job_id in {event.job_id for event in events}


async def test_only_stuck_jobs_can_be_replayed(ledger: Any) -> None:
    job_id = await _finish(ledger, "unit-done", JobStatus.SUCCEEDED)
    with pytest.raises(InvalidInputError, match="not replayable"):
        await ledger.replay(job_id, actor="ops", reason="again", now=START)
    with pytest.raises(InvalidInputError, match="Unknown job"):
        await ledger.replay("missing", actor="ops", reason="again", now=START)


async def _fail(ledger: Any, spec: JobSpec) -> None:
    await ledger.enqueue([spec], now=START)
    lease = await ledger.claim(spec.job_id, owner="w", lease_seconds=60, now=START)
    assert lease is not None
    outcome = StageOutcome(
        status=JobStatus.FAILED_PERMANENT, reason="429", counts_as_failure=True
    )
    await ledger.complete(lease, outcome, now=START)


async def test_permanent_failure_outside_a_plan_unit_replays_with_a_fresh_budget(
    ledger: Any,
) -> None:
    spec = JobSpec.create(
        stage=StageName.EMBED,
        pool=WorkerPool.IO,
        unit_id="evidence:e1",
        stage_version="1",
        inputs={"evidence_id": "evidence:e1"},
    )
    await _fail(ledger, spec)
    assert (await ledger.get(spec.job_id)).failure_count == 1
    later = START + timedelta(minutes=5)
    job = await ledger.replay(spec.job_id, actor="ops", reason="capacity raised", now=later)
    assert (job.status, job.failure_count) == (JobStatus.PENDING, 0)
    assert await ledger.claim(spec.job_id, owner="w2", lease_seconds=60, now=later) is not None


async def test_permanent_failure_that_settled_a_plan_unit_is_not_replayed(ledger: Any) -> None:
    unit = UnitRecord(unit_id="p9:u0", ordinal=0, kind="pdf_pages", route="di_layout", pages=(1,))
    plan = PlanRecord(
        plan_id="p9",
        content_hash="c" * 64,
        policy_version="routing-v1",
        family="pdf",
        page_count=1,
        units=(unit,),
    )
    spec = JobSpec.create(
        stage=StageName.DI_POLL,
        pool=WorkerPool.IO,
        unit_id=unit.unit_id,
        stage_version="1",
        inputs={"unit": unit.unit_id},
    )
    planner = JobSpec.create(
        stage=StageName.PLAN, pool=WorkerPool.CPU, unit_id="content:p9", stage_version="1"
    )
    await ledger.enqueue([planner], now=START)
    lease = await ledger.claim(planner.job_id, owner="w", lease_seconds=60, now=START)
    assert lease is not None
    await ledger.complete(lease, StageOutcome.succeeded(plan=plan), now=START)
    await _fail(ledger, spec)
    with pytest.raises(InvalidInputError, match="new policy"):
        await ledger.replay(spec.job_id, actor="ops", reason="again", now=START)
    assert (await ledger.get(spec.job_id)).status is JobStatus.FAILED_PERMANENT
