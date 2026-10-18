"""Stage handler contract and the per-execution context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from ..domain.errors import ConfigurationError
from ..domain.models import Lease, StageJob, StageOutcome
from ..domain.planning import JobPlanner
from ..faults import NO_FAULTS, FaultInjector
from ..ports.clock import Clock
from ..ports.evidence import (
    EmbeddingCache,
    EvidenceRepository,
    ExtractionCache,
    ProjectionRepository,
)
from ..ports.ledger import PlanReader, SnapshotRepository, WorkloadReader
from ..ports.parsing import DrawingReader, PdfToolkit, SpreadsheetReader, TokenCounter
from ..ports.providers import (
    AnalyzerCatalog,
    ContentAnalysisProvider,
    EmbeddingProvider,
    LayoutProvider,
    SearchProjector,
)
from ..ports.storage import ArtifactStore, SourceReader

T = TypeVar("T")


def _require(value: T | None, what: str) -> T:
    if value is None:
        raise ConfigurationError(f"This worker has no {what} configured")
    return value


@dataclass(frozen=True)
class AccountClients:
    """Paid-analysis clients of one external billing account."""

    layout: LayoutProvider | None = None
    content_analysis: ContentAnalysisProvider | None = None
    analyzers: frozenset[str] = frozenset()
    """Analyzers whose profile matched this subscription's at startup; only they get new work."""


@dataclass(frozen=True)
class StageServices:
    """Long-lived dependencies shared by every execution in one process."""

    clock: Clock
    planner: JobPlanner
    faults: FaultInjector = NO_FAULTS
    snapshots: SnapshotRepository | None = None
    sources: SourceReader | None = None
    artifacts: ArtifactStore | None = None
    plans: PlanReader | None = None
    workload: WorkloadReader | None = None
    work_dir: Path | None = None
    pdf: PdfToolkit | None = None
    sheets: SpreadsheetReader | None = None
    drawings: DrawingReader | None = None
    tokens: TokenCounter | None = None
    layout: LayoutProvider | None = None
    evidence: EvidenceRepository | None = None
    embedding_cache: EmbeddingCache | None = None
    projections: ProjectionRepository | None = None
    embeddings: EmbeddingProvider | None = None
    projector: SearchProjector | None = None
    content_analysis: ContentAnalysisProvider | None = None
    analyzers: AnalyzerCatalog | None = None
    extraction_cache: ExtractionCache | None = None
    """Optional: without it every paid analysis runs (no cross-policy reuse)."""
    accounts: Mapping[str, AccountClients] = field(default_factory=dict)
    """External billing accounts by name; the provider fields above are this subscription's."""

    def layout_for(self, account: str | None) -> LayoutProvider | None:
        if account is None:
            return self.layout
        clients = self.accounts.get(account)
        return None if clients is None else clients.layout

    def content_analysis_for(self, account: str | None) -> ContentAnalysisProvider | None:
        if account is None:
            return self.content_analysis
        clients = self.accounts.get(account)
        return None if clients is None else clients.content_analysis

    def accepts_cu_work(self, account: str | None, analyzer_id: str) -> bool:
        if account is None:
            return self.content_analysis is not None
        clients = self.accounts.get(account)
        return clients is not None and analyzer_id in clients.analyzers

    def require_snapshots(self) -> SnapshotRepository:
        return _require(self.snapshots, "snapshot repository")

    def require_sources(self) -> SourceReader:
        return _require(self.sources, "source reader")

    def require_artifacts(self) -> ArtifactStore:
        return _require(self.artifacts, "artifact store")

    def require_plans(self) -> PlanReader:
        return _require(self.plans, "plan reader")

    def require_workload(self) -> WorkloadReader:
        return _require(self.workload, "workload reader")

    def require_pdf(self) -> PdfToolkit:
        return _require(self.pdf, "PDF toolkit")

    def require_sheets(self) -> SpreadsheetReader:
        return _require(self.sheets, "spreadsheet reader")

    def require_drawings(self) -> DrawingReader:
        return _require(self.drawings, "DWG reader")

    def require_tokens(self) -> TokenCounter:
        return _require(self.tokens, "token counter")

    def require_layout(self, account: str | None = None) -> LayoutProvider:
        return _require(self.layout_for(account), _what("layout provider", account))

    def require_evidence(self) -> EvidenceRepository:
        return _require(self.evidence, "evidence repository")

    def require_embedding_cache(self) -> EmbeddingCache:
        return _require(self.embedding_cache, "embedding cache")

    def require_projections(self) -> ProjectionRepository:
        return _require(self.projections, "projection repository")

    def require_embeddings(self) -> EmbeddingProvider:
        return _require(self.embeddings, "embedding provider")

    def require_projector(self) -> SearchProjector:
        return _require(self.projector, "search projector")

    def require_content_analysis(self, account: str | None = None) -> ContentAnalysisProvider:
        return _require(
            self.content_analysis_for(account), _what("Content Understanding provider", account)
        )

    def require_analyzers(self) -> AnalyzerCatalog:
        return _require(self.analyzers, "verified analyzer catalog")


def _what(provider: str, account: str | None) -> str:
    return provider if account is None else f"{provider} for account {account}"


@dataclass(frozen=True)
class StageContext:
    services: StageServices
    lease: Lease


class StageHandler(Protocol):
    @property
    def version(self) -> str:
        """Bumping the version changes fingerprints of newly planned jobs."""
        ...

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        """Do one bounded action. Raise a ``PipelineError`` to let the retry policy decide."""
        ...
