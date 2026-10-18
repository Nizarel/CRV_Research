"""Billing accounts: paid analyses fill this subscription's budget, then the next account's."""

from __future__ import annotations

from pathlib import Path

import pytest
from evidence_fakes import PFD_ANALYZER, FakeContentAnalysis, FakeLayout
from evidence_harness import MAX_SOURCE, build_evidence_harness
from fixture_builders import A3_LANDSCAPE, PdfPage, pdf_bytes

from ingest_pipeline.domain.routing import CuProfileRule, RoutingPolicy, UnitKind
from ingest_pipeline.domain.spend import Prices
from ingest_pipeline.domain.states import JobStatus, StageName, WorkerPool
from ingest_pipeline.stages.base import AccountClients
from ingest_pipeline.stages.cu import CuSettings
from ingest_pipeline.stages.di import DiSettings
from ingest_pipeline.stages.spend import PAID_PROVIDERS, BillingAccount, SpendPolicy

CU_FIRST = RoutingPolicy(
    version="cu-first",
    cu_profiles=(
        CuProfileRule(unit_kinds=frozenset({UnitKind.DRAWING_SHEET}), analyzer_id=PFD_ANALYZER),
    ),
)
# The fake CU result of one page: 1 page, 1,000 contextualization, 1,000 input and 100 output
# tokens, so USD 0.008 per page.
PRICES = Prices(
    di_page_usd=0.01,
    cu_page_estimate_usd=0.01,
    cu_meter_usd={
        "documentPagesStandard": 0.005,
        "contextualizationTokens": 0.000001,
        "gpt-5.4-mini-input": 0.000001,
        "gpt-5.4-mini-output": 0.00001,
    },
)


def _drawings(count: int) -> bytes:
    return pdf_bytes([PdfPage(size=A3_LANDSCAPE, vector_lines=12) for _ in range(count)])


def _cu(*accounts: BillingAccount) -> CuSettings:
    return CuSettings(
        max_source_bytes=MAX_SOURCE, spend=SpendPolicy(accounts=accounts, prices=PRICES)
    )


async def test_cu_work_moves_to_the_next_account_once_the_budget_is_committed(
    tmp_path: Path,
) -> None:
    partner = FakeContentAnalysis()
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        cu=_cu(BillingAccount(budget_usd=0.012), BillingAccount("partner")),
        accounts={
            "partner": AccountClients(content_analysis=partner, analyzers=frozenset({PFD_ANALYZER}))
        },
    )
    await kit.ingest("PFD/pfd.pdf", _drawings(3))
    await kit.drain()

    assert len(kit.analysis.submissions) == 1
    assert len(partner.submissions) == 2
    assert sorted(partner.polls.values()) == [2, 2]
    assert (len(kit.analysis.deleted), len(partner.deleted)) == (1, 2)
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    ledger = kit.harness.ledger
    assert await ledger.account_spend(None, PAID_PROVIDERS) == pytest.approx(0.008)
    assert await ledger.account_spend("partner", PAID_PROVIDERS) == pytest.approx(0.016)


async def test_work_waits_when_every_account_is_at_its_budget(tmp_path: Path) -> None:
    partner = FakeContentAnalysis()
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        cu=_cu(BillingAccount(budget_usd=0.012), BillingAccount("partner", 0.012)),
        accounts={
            "partner": AccountClients(content_analysis=partner, analyzers=frozenset({PFD_ANALYZER}))
        },
    )
    await kit.ingest("PFD/pfd.pdf", _drawings(3))
    await kit.drain(max_rounds=40, strict=False)

    assert (len(kit.analysis.submissions), len(partner.submissions)) == (1, 1)
    [waiting] = [job for job in kit.jobs(StageName.CU_SUBMIT) if job.status is JobStatus.RETRY_WAIT]
    assert (waiting.last_error or "").startswith("Account budgets reached: this subscription")
    assert kit.statuses(StageName.CHUNK) == [JobStatus.BLOCKED]


async def test_an_account_with_its_own_slots_runs_in_parallel(tmp_path: Path) -> None:
    partner = FakeContentAnalysis(polls_before_done=2)
    accounts = (BillingAccount(), BillingAccount("partner", cu_max_inflight=1))
    cu = CuSettings(
        max_source_bytes=MAX_SOURCE, max_inflight=1, spend=SpendPolicy(accounts=accounts)
    )
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        cu=cu,
        analysis=FakeContentAnalysis(polls_before_done=2),
        accounts={
            "partner": AccountClients(content_analysis=partner, analyzers=frozenset({PFD_ANALYZER}))
        },
    )
    kit.harness.worker(WorkerPool.IO, concurrency=4)
    await kit.ingest("PFD/pfd.pdf", _drawings(4))
    await kit.drain(max_rounds=400)

    assert len(kit.analysis.submissions) >= 1
    assert len(partner.submissions) >= 1
    assert len(kit.analysis.submissions) + len(partner.submissions) == 4
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]


async def test_an_account_without_the_analyzer_gets_no_cu_work(tmp_path: Path) -> None:
    partner = FakeContentAnalysis()
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        cu=_cu(BillingAccount(budget_usd=0.012), BillingAccount("partner")),
        accounts={"partner": AccountClients(content_analysis=partner)},
    )
    await kit.ingest("PFD/pfd.pdf", _drawings(2))
    await kit.drain(max_rounds=40, strict=False)

    assert (len(kit.analysis.submissions), len(partner.submissions)) == (1, 0)
    assert JobStatus.RETRY_WAIT in kit.statuses(StageName.CU_SUBMIT)


async def test_layout_figures_and_cleanup_follow_the_account_that_ran_the_analysis(
    tmp_path: Path,
) -> None:
    partner = FakeLayout()
    spend = SpendPolicy(
        accounts=(BillingAccount(budget_usd=0.0), BillingAccount("partner")), prices=PRICES
    )
    kit = build_evidence_harness(
        tmp_path,
        di=DiSettings(max_source_bytes=MAX_SOURCE, spend=spend),
        accounts={"partner": AccountClients(layout=partner)},
    )
    await kit.ingest("Plans/drawing.pdf", _drawings(1))
    await kit.drain()

    assert not kit.layout.submissions
    assert len(partner.submissions) == 1
    assert partner.figure_calls
    assert not kit.layout.figure_calls
    assert partner.deleted
    assert not kit.layout.deleted
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    ledger = kit.harness.ledger
    assert await ledger.account_spend("partner", PAID_PROVIDERS) == pytest.approx(0.01)
    assert await ledger.account_spend(None, PAID_PROVIDERS) == 0
