"""Plan barrier (fan-in) contract shared by the in-memory and PostgreSQL ledgers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from pipeline_harness import START

from ingest_pipeline.adapters.memory.ledger import InMemoryLedger
from ingest_pipeline.domain.errors import ErrorCategory, IntegrityError
from ingest_pipeline.domain.models import (
    ArtifactRef,
    JobSpec,
    PlanRecord,
    StageOutcome,
    UnitDisposition,
    UnitRecord,
    UnitResult,
)
from ingest_pipeline.domain.states import JobStatus, StageName, WorkerPool

pytestmark = pytest.mark.contract

CONTENT = "c" * 64


@dataclass
class Backend:
    ledger: Any


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def backend(request: pytest.FixtureRequest) -> Backend:
    if request.param == "memory":
        return Backend(InMemoryLedger())
    from ingest_pipeline.adapters.postgres.ledger import PostgresLedger

    return Backend(PostgresLedger(request.getfixturevalue("pg_pool")))


def _at(seconds: float) -> Any:
    return START + timedelta(seconds=seconds)


def _spec(stage: StageName, unit_id: str, **fields: Any) -> JobSpec:
    return JobSpec.create(
        stage=stage,
        pool=WorkerPool.CPU,
        unit_id=unit_id,
        stage_version="1",
        inputs={"unit": unit_id},
        max_failures=fields.pop("max_failures", 3),
        **fields,
    )


def _plan(plan_id: str, units: int, *, preset: dict[int, UnitDisposition] | None = None):
    preset = preset or {}
    return PlanRecord(
        plan_id=plan_id,
        content_hash=CONTENT,
        policy_version="routing-v1",
        family="pdf",
        page_count=units,
        units=tuple(
            UnitRecord(
                unit_id=f"{plan_id}:u{index}",
                ordinal=index,
                kind="pdf_pages",
                route="native_pdf_text",
                pages=(index + 1,),
                disposition=preset.get(index),
                reason="preset" if index in preset else None,
            )
            for index in range(units)
        ),
    )


def _evidence(index: int) -> ArtifactRef:
    digest = f"{index:x}".rjust(64, "e")
    return ArtifactRef(
        sha256=digest, uri=f"sha256/{digest[:2]}/{digest}", size_bytes=10, media_type="x/json"
    )


async def _run(backend: Backend, spec: JobSpec, outcome: StageOutcome, *, at: float) -> None:
    lease = await backend.ledger.claim(spec.job_id, owner="w", lease_seconds=60, now=_at(at))
    assert lease is not None, f"{spec.stage.value} job was not claimable"
    await backend.ledger.complete(lease, outcome, now=_at(at))


async def _register(
    backend: Backend, plan: PlanRecord, *, unit_jobs: bool = True
) -> tuple[list[JobSpec], JobSpec]:
    planner = _spec(StageName.PLAN, f"content:{plan.plan_id}")
    await backend.ledger.enqueue([planner], now=START)
    units = [
        _spec(StageName.NATIVE_PARSE, unit.unit_id)
        for unit in plan.units
        if unit.disposition is None and unit_jobs
    ]
    barrier = _spec(StageName.CHUNK, f"plan:{plan.plan_id}", barrier_plan_id=plan.plan_id)
    await _run(
        backend,
        planner,
        StageOutcome.succeeded(plan=plan, next_jobs=(*units, barrier)),
        at=1,
    )
    return units, barrier


async def _due_jobs(backend: Backend, at: float) -> list[str]:
    events = await backend.ledger.lease_batch(now=_at(at), limit=100, claim_seconds=30)
    return [event.job_id for event in events]


async def test_barrier_job_is_blocked_and_never_dispatched(backend: Backend) -> None:
    units, barrier = await _register(backend, _plan("p1", 2))

    job = await backend.ledger.get(barrier.job_id)
    assert job.status is JobStatus.BLOCKED
    due = await _due_jobs(backend, 2)
    assert barrier.job_id not in due
    assert set(due) >= {unit.job_id for unit in units}
    claimed = await backend.ledger.claim(barrier.job_id, owner="w", lease_seconds=60, now=_at(3))
    assert claimed is None


async def test_barrier_releases_once_after_every_unit_settles(backend: Backend) -> None:
    plan = _plan("p2", 3)
    units, barrier = await _register(backend, plan)
    await _due_jobs(backend, 2)

    await _run(
        backend,
        units[0],
        StageOutcome.succeeded(
            unit_result=UnitResult(disposition=UnitDisposition.EVIDENCE, evidence=_evidence(0))
        ),
        at=3,
    )
    await _run(
        backend,
        units[1],
        StageOutcome.succeeded(
            unit_result=UnitResult(disposition=UnitDisposition.NO_EVIDENCE, reason="blank")
        ),
        at=4,
    )
    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.BLOCKED
    assert await _due_jobs(backend, 4) == []

    await _run(
        backend,
        units[2],
        StageOutcome(
            status=JobStatus.FAILED_PERMANENT,
            reason="corrupt page",
            error_category=ErrorCategory.INVALID_INPUT,
        ),
        at=5,
    )

    released = await backend.ledger.get(barrier.job_id)
    assert (released.status, released.not_before) == (JobStatus.PENDING, _at(5))
    assert await _due_jobs(backend, 5) == [barrier.job_id]
    states = await backend.ledger.plan_units("p2")
    assert [(s.unit.ordinal, s.disposition, s.unit.reason) for s in states] == [
        (0, UnitDisposition.EVIDENCE, None),
        (1, UnitDisposition.NO_EVIDENCE, "blank"),
        (2, UnitDisposition.FAILED, "corrupt page"),
    ]
    assert states[0].evidence == _evidence(0)
    assert await backend.ledger.release_ready_barriers(now=_at(6), limit=10) == 0


async def test_intermediate_success_does_not_settle_the_unit(backend: Backend) -> None:
    plan = _plan("p3", 1)
    [unit], barrier = await _register(backend, plan)
    poll = _spec(StageName.DI_POLL, unit.unit_id)

    await _run(backend, unit, StageOutcome.succeeded(next_jobs=(poll,)), at=2)

    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.BLOCKED
    assert (await backend.ledger.plan_units("p3"))[0].disposition is None
    await _run(
        backend,
        poll,
        StageOutcome.succeeded(
            unit_result=UnitResult(disposition=UnitDisposition.EVIDENCE, evidence=_evidence(1))
        ),
        at=3,
    )
    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.PENDING


async def test_fully_preset_plan_releases_in_the_registering_transaction(
    backend: Backend,
) -> None:
    plan = _plan(
        "p4", 2, preset={0: UnitDisposition.NO_EVIDENCE, 1: UnitDisposition.UNSUPPORTED}
    )
    _, barrier = await _register(backend, plan)

    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.PENDING
    stored = await backend.ledger.plan("p4")
    assert [unit.disposition for unit in stored.units] == [
        UnitDisposition.NO_EVIDENCE,
        UnitDisposition.UNSUPPORTED,
    ]


async def test_settled_unit_keeps_its_first_disposition(backend: Backend) -> None:
    plan = _plan("p5", 1)
    [unit], _ = await _register(backend, plan)
    retry = _spec(StageName.NORMALIZE, unit.unit_id)
    await _run(
        backend,
        unit,
        StageOutcome.succeeded(
            unit_result=UnitResult(disposition=UnitDisposition.NO_EVIDENCE),
            next_jobs=(retry,),
        ),
        at=2,
    )
    await _run(
        backend,
        retry,
        StageOutcome(status=JobStatus.QUARANTINED, error_category=ErrorCategory.INVALID_INPUT),
        at=3,
    )

    [state] = await backend.ledger.plan_units("p5")
    assert state.disposition is UnitDisposition.NO_EVIDENCE


async def test_exhausted_lease_recovery_fails_the_unit_and_releases(backend: Backend) -> None:
    plan = _plan("p6", 1)
    units = [_spec(StageName.NATIVE_PARSE, unit.unit_id, max_failures=1) for unit in plan.units]
    planner = _spec(StageName.PLAN, "content:p6")
    barrier = _spec(StageName.CHUNK, "plan:p6", barrier_plan_id="p6")
    await backend.ledger.enqueue([planner], now=START)
    await _run(
        backend, planner, StageOutcome.succeeded(plan=plan, next_jobs=(*units, barrier)), at=1
    )
    lease = await backend.ledger.claim(units[0].job_id, owner="w", lease_seconds=60, now=_at(2))
    assert lease is not None

    result = await backend.ledger.recover_expired(now=_at(100), limit=10)

    assert result.failed == 1
    [state] = await backend.ledger.plan_units("p6")
    assert state.disposition is UnitDisposition.FAILED
    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.PENDING


async def test_concurrent_last_completions_release_exactly_once(backend: Backend) -> None:
    plan = _plan("p7", 4)
    units, barrier = await _register(backend, plan)
    leases = [
        await backend.ledger.claim(unit.job_id, owner=f"w{i}", lease_seconds=60, now=_at(2))
        for i, unit in enumerate(units)
    ]
    outcome = StageOutcome.succeeded(
        unit_result=UnitResult(disposition=UnitDisposition.NO_EVIDENCE, reason="blank")
    )

    await asyncio.gather(
        *(backend.ledger.complete(lease, outcome, now=_at(3)) for lease in leases)
    )

    assert (await backend.ledger.get(barrier.job_id)).status is JobStatus.PENDING
    due = await _due_jobs(backend, 3)
    assert due.count(barrier.job_id) == 1


async def test_barrier_on_unknown_plan_is_rejected(backend: Backend) -> None:
    orphan = _spec(StageName.CHUNK, "plan:missing", barrier_plan_id="missing")
    with pytest.raises(IntegrityError):
        await backend.ledger.enqueue([orphan], now=START)
