"""Read-only status report SQL against a real PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from pipeline_harness import START

from tizertmaint_pipeline.adapters.postgres.ledger import PostgresLedger
from tizertmaint_pipeline.adapters.postgres.report import pipeline_status
from tizertmaint_pipeline.domain.models import JobSpec, StageOutcome
from tizertmaint_pipeline.domain.states import JobStatus, StageName, WorkerPool

pytestmark = pytest.mark.integration


async def test_status_report_summarizes_jobs_and_problems(pg_pool: Any) -> None:
    ledger = PostgresLedger(pg_pool)
    spec = JobSpec.create(
        stage=StageName.DI_SUBMIT, pool=WorkerPool.IO, unit_id="u-status", stage_version="1"
    )
    await ledger.enqueue([spec], now=START)
    lease = await ledger.claim(spec.job_id, owner="w", lease_seconds=60, now=START)
    assert lease is not None
    await ledger.complete(
        lease, StageOutcome(status=JobStatus.NEEDS_REVIEW, reason="check portal"), now=START
    )

    report = await pipeline_status(pg_pool, failures=5)

    assert set(report) == {
        "jobs", "units", "provider_usage", "evidence", "projections", "figures", "problems",
    }
    assert {"stage": "di_submit", "status": "needs_review", "jobs": 1} in report["jobs"]
    assert report["problems"][0]["job_id"] == spec.job_id
    assert report["problems"][0]["last_error"] == "check portal"
