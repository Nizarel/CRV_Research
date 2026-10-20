"""Content Understanding flows: CU-first, selective escalation, result reuse and spend caps."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from evidence_fakes import LAYOUT_ANALYZER, PFD_ANALYZER, FakeContentAnalysis, plain_analysis
from evidence_harness import MAX_SOURCE, EvidenceKit, build_evidence_harness
from fixture_builders import A3_LANDSCAPE, PdfPage, pdf_bytes

from ingest_pipeline.domain.cu import PROVIDER
from ingest_pipeline.domain.routing import (
    CuProfileRule,
    EscalationRule,
    RoutingPolicy,
    UnitKind,
)
from ingest_pipeline.domain.states import JobStatus, StageName, WorkerPool
from ingest_pipeline.ports.providers import (
    AnalysisPoll,
    AnalysisRequest,
    AnalysisStatus,
    LayoutSubmission,
)
from ingest_pipeline.stages.cu import CuSettings

DRAWINGS = frozenset({UnitKind.DRAWING_SHEET})
CU_FIRST = RoutingPolicy(
    version="bakeoff-cu-first",
    cu_profiles=(CuProfileRule(unit_kinds=DRAWINGS, analyzer_id=PFD_ANALYZER),),
)
TAG = "6140-ZM-2427"


def text_page() -> PdfPage:
    return PdfPage(
        lines=[
            "Le broyeur primaire BR-101 est alimente par le convoyeur CV-12.",
            "Verifier le serrage des boulons de fixation chaque semaine.",
        ]
    )


def _drawing() -> PdfPage:
    return PdfPage(size=A3_LANDSCAPE, vector_lines=12)


def _selective(min_words: int) -> RoutingPolicy:
    return RoutingPolicy(
        version=f"bakeoff-selective-{min_words}",
        escalation=(
            EscalationRule(
                unit_kinds=DRAWINGS, analyzer_id=PFD_ANALYZER, min_words_per_page=min_words
            ),
        ),
    )


def _page_rows(kit: EvidenceKit, page: int) -> list[dict]:
    return [row for row in kit.index.rows.values() if row["original_page_numbers"] == [page]]


async def test_cu_first_drawings_skip_di_and_keep_generated_text_apart(tmp_path: Path) -> None:
    kit = build_evidence_harness(
        tmp_path, policy=CU_FIRST, release_ids={CU_FIRST.version: "bakeoff-cu-first-v1"}
    )
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([text_page(), _drawing()]))
    await kit.drain()

    assert not kit.layout.submissions
    [(request, _)] = kit.analysis.submissions
    assert (request.analyzer_id, request.content_range) == (PFD_ANALYZER, "2")
    assert request.mime_type == "application/pdf"
    assert request.processing_location == "dataZone"
    assert kit.statuses(StageName.CU_POLL) == [JobStatus.SUCCEEDED]
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    assert kit.statuses(StageName.FIGURE_ASSETS) == [JobStatus.SUCCEEDED]
    assert kit.analysis.deleted
    assert not kit.analysis.file_calls

    rows = list(kit.index.rows.values())
    assert {row["release_id"] for row in rows} == {"bakeoff-cu-first-v1"}
    drawing = _page_rows(kit, 2)
    generated = [row for row in drawing if row["is_generated"]]
    source = [row for row in drawing if not row["is_generated"]]
    assert generated
    assert {row["evidence_kind"] for row in generated} == {"generated_description"}
    assert any(TAG in row["content"] for row in source)
    assert not any("Schema du circuit" in row["content"] for row in source)
    assert all(TAG in row["mentioned_tags"] and TAG in row["identifiers"] for row in drawing)
    assert {row["extraction_route"] for row in drawing} == {"cu_analyze"}
    assert not any(TAG in row["identifiers"] for row in _page_rows(kit, 1))

    usage = await kit.harness.ledger.provider_usage(PROVIDER)
    assert (usage.pages, usage.input_tokens, usage.output_tokens) == (1, 1000, 100)


async def test_selective_escalates_only_units_failing_the_gate(tmp_path: Path) -> None:
    kit = build_evidence_harness(tmp_path, policy=_selective(50))
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([text_page(), _drawing()]))
    await kit.drain()

    assert len(kit.layout.submissions) == 1
    assert len(kit.analysis.submissions) == 1
    normalize = kit.jobs(StageName.NORMALIZE)
    assert [job.status for job in normalize] == [JobStatus.SUCCEEDED] * 2
    escalated = [job for job in normalize if job.checkpoint.get("escalated_to")]
    assert [job.checkpoint["escalated_to"] for job in escalated] == [PFD_ANALYZER]
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    drawing = "\n".join(row["content"] for row in _page_rows(kit, 2))
    assert TAG in drawing
    assert "pompe de circulation" not in drawing


async def test_units_passing_the_gate_stay_on_di(tmp_path: Path) -> None:
    kit = build_evidence_harness(tmp_path, policy=_selective(1))
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([text_page(), _drawing()]))
    await kit.drain()

    assert len(kit.layout.submissions) == 1
    assert not kit.analysis.submissions
    assert not kit.jobs(StageName.CU_SUBMIT)
    assert _page_rows(kit, 2)


async def test_arms_sharing_an_analysis_reuse_the_paid_result(tmp_path: Path) -> None:
    other = CU_FIRST.model_copy(update={"version": "bakeoff-cu-first-b"})
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        release_ids={CU_FIRST.version: "arm-a", other.version: "arm-b"},
    )
    data = pdf_bytes([text_page(), _drawing()])
    await kit.ingest("PFD/pfd.pdf", data)
    await kit.drain()

    await kit.enqueue_arm(other, hashlib.sha256(data).hexdigest())
    await kit.drain()

    assert len(kit.analysis.submissions) == 1
    submits = kit.jobs(StageName.CU_SUBMIT)
    assert len(submits) == 2
    assert sum(1 for job in submits if job.checkpoint.get("reused")) == 1
    assert {row["release_id"] for row in kit.index.rows.values()} == {"arm-a", "arm-b"}
    usage = await kit.harness.ledger.provider_usage(PROVIDER)
    assert usage.pages == 1


@dataclass
class _CountingAnalysis(FakeContentAnalysis):
    open: set[str] = field(default_factory=set)
    peak: int = 0

    async def submit(self, source: Path, request: AnalysisRequest) -> LayoutSubmission:
        submission = await super().submit(source, request)
        self.open.add(submission.operation_url)
        self.peak = max(self.peak, len(self.open))
        return submission

    async def poll(self, operation_url: str, destination: Path) -> AnalysisPoll:
        result = await super().poll(operation_url, destination)
        if result.status is not AnalysisStatus.RUNNING:
            self.open.discard(operation_url)
        return result


async def test_concurrent_submits_fill_every_slot_without_exceeding_it(tmp_path: Path) -> None:
    analysis = _CountingAnalysis(polls_before_done=2)
    kit = build_evidence_harness(
        tmp_path,
        policy=CU_FIRST,
        cu=CuSettings(max_source_bytes=MAX_SOURCE, max_inflight=2, admission_wait_seconds=5),
        analysis=analysis,
    )
    kit.harness.worker(WorkerPool.IO, concurrency=4)
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([_drawing()] * 5))
    await kit.drain(max_rounds=400)

    assert len(analysis.submissions) == 5
    assert analysis.peak == 2
    assert kit.statuses(StageName.CU_SUBMIT) == [JobStatus.SUCCEEDED] * 5
    # Parked submits are woken by the slot they wait for, not by retry timers.
    for job in kit.jobs(StageName.CU_SUBMIT):
        assert len(await kit.harness.ledger.attempts(job.job_id)) <= 3


async def test_page_budget_holds_work_back_instead_of_overspending(tmp_path: Path) -> None:
    kit = build_evidence_harness(
        tmp_path, policy=CU_FIRST, cu=CuSettings(max_source_bytes=MAX_SOURCE, page_budget=1)
    )
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([_drawing(), _drawing()]))
    await kit.drain(max_rounds=40, strict=False)

    assert len(kit.analysis.submissions) == 1
    waiting = [job for job in kit.jobs(StageName.CU_SUBMIT) if job.status is JobStatus.RETRY_WAIT]
    assert len(waiting) == 1
    assert "CU page budget 1 reached" in (waiting[0].last_error or "")
    assert kit.statuses(StageName.CHUNK) == [JobStatus.BLOCKED]


async def test_token_budget_reserves_in_flight_work_then_uses_real_usage(tmp_path: Path) -> None:
    settings = CuSettings(
        max_source_bytes=MAX_SOURCE, token_budget=20_000, est_tokens_per_page=12_000
    )
    kit = build_evidence_harness(tmp_path, policy=CU_FIRST, cu=settings)
    await kit.ingest("PFD/pfd.pdf", pdf_bytes([_drawing(), _drawing()]))
    await kit.drain()

    assert len(kit.analysis.submissions) == 2
    assert kit.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    usage = await kit.harness.ledger.provider_usage(PROVIDER)
    assert usage.input_tokens + usage.output_tokens == 2200


async def test_zero_token_budget_admits_extraction_only_and_blocks_generative(
    tmp_path: Path,
) -> None:
    settings = CuSettings(max_source_bytes=MAX_SOURCE, page_budget=10, token_budget=0)
    layout_first = RoutingPolicy(
        version="crv-cu-layout",
        cu_profiles=(CuProfileRule(unit_kinds=DRAWINGS, analyzer_id=LAYOUT_ANALYZER),),
    )
    extraction = build_evidence_harness(
        tmp_path / "extract", policy=layout_first, cu=settings,
        analysis=FakeContentAnalysis(responder=plain_analysis),
    )
    await extraction.ingest("Plans/a.pdf", pdf_bytes([_drawing(), _drawing()]))
    await extraction.drain()
    assert [request.analyzer_id for request, _ in extraction.analysis.submissions] == [
        LAYOUT_ANALYZER, LAYOUT_ANALYZER,
    ]
    assert extraction.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]

    generative = build_evidence_harness(tmp_path / "generate", policy=CU_FIRST, cu=settings)
    await generative.ingest("PFD/pfd.pdf", pdf_bytes([_drawing()]))
    await generative.drain(strict=False)
    assert not generative.analysis.submissions
    [waiting] = generative.jobs(StageName.CU_SUBMIT)
    assert "CU token budget 0 reached" in (waiting.last_error or "")


async def test_throttled_analyses_are_resubmitted_then_fail_the_unit(tmp_path: Path) -> None:
    settings = CuSettings(max_source_bytes=MAX_SOURCE, max_resubmits=2)
    flaky = build_evidence_harness(
        tmp_path / "flaky", policy=CU_FIRST, cu=settings,
        analysis=FakeContentAnalysis(transient_failures=1),
    )
    await flaky.ingest("PFD/pfd.pdf", pdf_bytes([_drawing()]))
    await flaky.drain()
    assert len(flaky.analysis.submissions) == 2
    assert flaky.statuses(StageName.CU_POLL) == [JobStatus.SUCCEEDED] * 2
    assert flaky.statuses(StageName.CHUNK) == [JobStatus.SUCCEEDED]
    usage = await flaky.harness.ledger.provider_usage(PROVIDER)
    assert usage.pages == 2

    broken = build_evidence_harness(
        tmp_path / "broken", policy=CU_FIRST, cu=settings,
        analysis=FakeContentAnalysis(transient_failures=10),
    )
    await broken.ingest("PFD/pfd.pdf", pdf_bytes([_drawing()]))
    await broken.drain()
    assert len(broken.analysis.submissions) == 3
    assert broken.statuses(StageName.CU_POLL) == [
        JobStatus.FAILED_PERMANENT,
        JobStatus.SUCCEEDED,
        JobStatus.SUCCEEDED,
    ]
    assert broken.statuses(StageName.CHUNK) == [JobStatus.NEEDS_REVIEW]
