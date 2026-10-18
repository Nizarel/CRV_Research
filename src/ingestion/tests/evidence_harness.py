"""Harness with every Phase 2 evidence stage wired to deterministic fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from evidence_fakes import (
    FakeContentAnalysis,
    FakeEmbeddings,
    FakeIndex,
    FakeLayout,
    fake_catalog,
)
from fixture_builders import index_fields
from pipeline_harness import Harness, build_harness

from ingest_pipeline.adapters.memory.evidence import InMemoryEvidenceStore
from ingest_pipeline.adapters.parsers.calamine import CalamineSpreadsheetReader
from ingest_pipeline.adapters.parsers.pdfium import PdfiumToolkit
from ingest_pipeline.adapters.parsers.tokenizers import ApproximateCounter
from ingest_pipeline.domain.models import StageJob
from ingest_pipeline.domain.routing import RoutingPolicy
from ingest_pipeline.domain.states import JobStatus, StageName
from ingest_pipeline.ports.parsing import DrawingReader
from ingest_pipeline.stages.base import AccountClients
from ingest_pipeline.stages.chunk import ChunkStage
from ingest_pipeline.stages.chunkers import ChunkingSettings
from ingest_pipeline.stages.cu import CuPollStage, CuSettings, CuSubmitStage
from ingest_pipeline.stages.di import (
    DiPollStage,
    DiSettings,
    DiSubmitStage,
    FigureAssetsStage,
)
from ingest_pipeline.stages.embed import EmbedStage
from ingest_pipeline.stages.extract import NativeParseStage, NormalizeStage
from ingest_pipeline.stages.links import PlacementRouter, preflight_job, project_job
from ingest_pipeline.stages.plan import LayoutOptions, PlanStage
from ingest_pipeline.stages.preflight import PreflightStage
from ingest_pipeline.stages.project_search import ProjectionSettings, ProjectSearchStage

INDEX = "maint-evidence-test"
MAX_SOURCE = 8 * 1024 * 1024


@dataclass
class EvidenceKit:
    harness: Harness
    layout: FakeLayout
    embeddings: FakeEmbeddings
    index: FakeIndex
    store: InMemoryEvidenceStore
    policy: RoutingPolicy
    settings: ProjectionSettings = field(default_factory=lambda: ProjectionSettings(target=INDEX))
    analysis: FakeContentAnalysis = field(default_factory=FakeContentAnalysis)

    async def ingest(self, blob_name: str, data: bytes) -> None:
        await self.harness.plan_snapshot(blob_name, data)

    async def enqueue_arm(self, policy: RoutingPolicy, content_hash: str) -> None:
        """Plan existing content under another routing policy (a bake-off arm)."""

        planner = self.harness.services.planner
        await self.harness.ledger.enqueue(
            [
                preflight_job(planner, content_hash, policy),
                project_job(planner, content_hash=content_hash, policy_version=policy.version),
            ],
            now=self.harness.clock.now(),
        )

    async def drain(self, max_rounds: int = 200, *, strict: bool = True) -> None:
        await self.harness.drain(max_rounds=max_rounds, strict=strict)

    def jobs(self, stage: StageName | None = None) -> list[StageJob]:
        return [
            job
            for job in self.harness.ledger.jobs()
            if stage is None or job.stage is stage
        ]

    def statuses(self, stage: StageName) -> list[JobStatus]:
        return sorted(job.status for job in self.jobs(stage))


def build_evidence_harness(
    work_dir: Path,
    *,
    layout: FakeLayout | None = None,
    policy: RoutingPolicy | None = None,
    di: DiSettings | None = None,
    chunking: ChunkingSettings | None = None,
    layout_options: LayoutOptions | None = None,
    max_failures: int = 3,
    analysis: FakeContentAnalysis | None = None,
    cu: CuSettings | None = None,
    release_ids: dict[str, str] | None = None,
    drawings: DrawingReader | None = None,
    accounts: dict[str, AccountClients] | None = None,
    corpus: Literal["maintenance", "crv"] = "maintenance",
    fields: frozenset[str] | None = None,
) -> EvidenceKit:
    layout = layout or FakeLayout()
    analysis = analysis or FakeContentAnalysis()
    policy = policy or RoutingPolicy()
    di = di or DiSettings(max_source_bytes=MAX_SOURCE)
    cu = cu or CuSettings(max_source_bytes=MAX_SOURCE)
    embeddings = FakeEmbeddings()
    index = FakeIndex(fields=fields if fields is not None else index_fields())
    store = InMemoryEvidenceStore()
    projection = ProjectionSettings(
        target=INDEX, retry_seconds=5, verify_deadline_seconds=60, release_ids=release_ids or {},
        corpus=corpus,
    )
    harness = build_harness(
        max_failures=max_failures,
        max_source_bytes=MAX_SOURCE,
        successors=PlacementRouter(policy),
        stages={
            StageName.PREFLIGHT: PreflightStage(max_source_bytes=MAX_SOURCE),
            StageName.PLAN: PlanStage(layout_options or LayoutOptions()),
            StageName.NATIVE_PARSE: NativeParseStage(),
            StageName.DI_SUBMIT: DiSubmitStage(di),
            StageName.DI_POLL: DiPollStage(di),
            StageName.CU_SUBMIT: CuSubmitStage(cu),
            StageName.CU_POLL: CuPollStage(cu),
            StageName.FIGURE_ASSETS: FigureAssetsStage(di),
            StageName.NORMALIZE: NormalizeStage(),
            StageName.CHUNK: ChunkStage(chunking or ChunkingSettings()),
            StageName.EMBED: EmbedStage(batch_size=4),
            StageName.PROJECT_SEARCH: ProjectSearchStage(projection),
        },
        extra_services={
            "work_dir": work_dir,
            "pdf": PdfiumToolkit(),
            "sheets": CalamineSpreadsheetReader(),
            "drawings": drawings,
            "tokens": ApproximateCounter(),
            "layout": layout,
            "evidence": store,
            "embedding_cache": store,
            "projections": store,
            "embeddings": embeddings,
            "projector": index,
            "content_analysis": analysis,
            "analyzers": fake_catalog(),
            "extraction_cache": store,
            "accounts": accounts or {},
        },
    )
    return EvidenceKit(
        harness=harness,
        layout=layout,
        embeddings=embeddings,
        index=index,
        store=store,
        policy=policy,
        settings=projection,
        analysis=analysis,
    )
