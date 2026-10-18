"""Document Intelligence stages: admission-controlled submit, durable polling, figure capture.

Remote operations survive worker restarts: submit records the Operation-Location in the
ledger (``OperationRef``) before polling starts, polls run as rescheduled jobs (never a
sleeping worker) and results are streamed to disk then stored immutably before normalizing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import AwareDatetime

from ..domain import cu
from ..domain.errors import (
    ErrorCategory,
    InvalidInputError,
    PipelineError,
    RemoteResultMissingError,
    UnsupportedContentError,
)
from ..domain.models import (
    ArtifactLink,
    ArtifactRef,
    OperationRef,
    ProviderUsage,
    StageJob,
    StageOutcome,
)
from ..domain.routing import SubmitMode
from ..domain.states import JobStatus, StageName
from ..faults import FaultPoint
from ..ports.evidence import EvidenceRepository, FigureAsset, FigureState
from ..ports.providers import (
    ContentAnalysisProvider,
    LayoutProvider,
    LayoutRequest,
    LayoutStatus,
)
from .base import StageContext
from .common import JSON_MEDIA_TYPE, fetch_content, parse_inputs, scratch
from .links import UnitInputs, _Inputs, unit_job
from .spend import DI_PROVIDER, SpendPolicy, start_on_account

PROVIDER = DI_PROVIDER
RAW_SCHEMA = "di-analyze-operation.v4"
STAGE_VERSION = "1"


class DiInputs(UnitInputs):
    request: dict[str, Any]
    submit_mode: SubmitMode


class DiPollInputs(DiInputs):
    operation_url: str
    submitted_at: AwareDatetime
    billable_pages: int
    page_map: dict[str, int] | None = None
    """Subset page position (as a string key) -> original 1-based page."""
    account: str | None = None
    """Billing account that accepted the operation; ``None`` is this subscription."""


class NormalizeInputs(UnitInputs):
    raw: ArtifactRef
    request: dict[str, Any]
    page_map: dict[str, int] | None = None
    provider: str = PROVIDER
    profile: dict[str, Any] | None = None
    """Content Understanding analyzer profile (field methods included)."""


class FigureInputs(_Inputs):
    unit_id: str
    content_hash: str
    operation_url: str
    submitted_at: AwareDatetime
    raw: ArtifactRef
    provider: str = PROVIDER
    capture: bool = True
    """``False`` only deletes the remote result."""
    account: str | None = None


@dataclass(frozen=True)
class DiSettings:
    max_source_bytes: int = 500 * 1024 * 1024
    max_inflight: int = 8
    page_budget: int | None = None
    """Hard cap on billable pages for this deployment; ``None`` means no cap."""
    admission_wait_seconds: float = 30.0
    park_seconds: float = 1800.0
    """Safety timer for a submit parked on the in-flight limit; a freed slot wakes it sooner."""
    poll_schedule: tuple[float, ...] = (2.0, 5.0, 13.0, 34.0, 60.0)
    poll_deadline_seconds: float = 3600.0
    result_retention_seconds: float = 23 * 3600.0
    """DI keeps results for 24 hours; figure capture gives up a margin before that."""
    spend: SpendPolicy = field(default_factory=SpendPolicy)


def request_from(identity: dict[str, Any]) -> LayoutRequest:
    return LayoutRequest(
        model_id=str(identity["model_id"]),
        pages=identity.get("pages"),
        output_content_format=str(identity["output_content_format"]),
        string_index_type=str(identity["string_index_type"]),
        figures=bool(identity["figures"]),
        features=tuple(identity.get("features") or ()),
        locale=identity.get("locale"),
    )


def _operation(
    layout: LayoutProvider, url: str, submitted_at: datetime, request_id: str | None = None
) -> OperationRef:
    return OperationRef(
        provider=PROVIDER,
        operation_url=url,
        api_version=layout.api_version,
        submitted_at=submitted_at,
        request_id=request_id,
    )


def throttled(now: datetime, seconds: float, reason: str) -> StageOutcome:
    return StageOutcome(
        status=JobStatus.RETRY_WAIT,
        reason=reason,
        error_category=ErrorCategory.THROTTLED,
        not_before=now + timedelta(seconds=seconds),
        retry_after_seconds=seconds,
    )


def parked(now: datetime, seconds: float, reason: str) -> StageOutcome:
    return StageOutcome(
        status=JobStatus.RETRY_WAIT,
        reason=reason,
        error_category=ErrorCategory.SLOT_WAIT,
        not_before=now + timedelta(seconds=seconds),
        retry_after_seconds=seconds,
    )


@dataclass(frozen=True)
class DiSubmitStage:
    settings: DiSettings = DiSettings()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(DiInputs, job)
        services = context.services
        workload = services.require_workload()
        now = services.clock.now()
        billable = max(len(inputs.unit.pages), 1)
        choices = await self.settings.spend.candidates(
            workload,
            provider=PROVIDER,
            pages=billable,
            page_usd=lambda prices: prices.di_page_usd,
            serves=lambda account: services.layout_for(account) is not None,
        )
        if choices.blocked is not None:
            return throttled(now, self.settings.admission_wait_seconds * 10, choices.blocked)
        admitted, account = await start_on_account(
            context,
            choices.accounts,
            provider=PROVIDER,
            polls=StageName.DI_POLL,
            default_limit=self.settings.max_inflight,
        )
        if not admitted:
            return parked(now, self.settings.park_seconds, "DI in-flight limit reached")
        budget = self.settings.page_budget
        if budget is not None:
            active = await workload.active_work(StageName.DI_POLL)
            used = await workload.provider_pages(PROVIDER)
            if used + active.billable_pages + billable > budget:
                return throttled(
                    now,
                    self.settings.admission_wait_seconds * 10,
                    f"DI page budget {budget} reached "
                    f"({used} used, {active.billable_pages} in flight)",
                )
        layout = services.require_layout(account)
        page_map: dict[str, int] | None = None
        async with scratch(services) as work:
            source = work / "source"
            await fetch_content(
                services, inputs.content_hash, source, max_bytes=self.settings.max_source_bytes
            )
            submit_path = source
            if inputs.submit_mode is SubmitMode.SUBSET:
                submit_path = work / "subset.pdf"
                await services.require_pdf().write_subset(source, inputs.unit.pages, submit_path)
                page_map = {str(index): page for index, page in enumerate(inputs.unit.pages, 1)}
            limit = inputs.unit.locator.get("max_submit_bytes")
            if limit is not None and submit_path.stat().st_size > int(limit):
                raise UnsupportedContentError("Serialized DI input exceeds its planned byte limit")
            submission = await layout.submit(submit_path, request_from(inputs.request))
        services.faults.hit(FaultPoint.AFTER_REMOTE_ACCEPT)
        submitted_at = services.clock.now()
        poll = DiPollInputs.model_validate(
            {
                **inputs.model_dump(mode="json"),
                "operation_url": submission.operation_url,
                "submitted_at": submitted_at,
                "billable_pages": billable,
                "page_map": page_map,
                "account": account,
            }
        )
        delay = max(self.settings.poll_schedule[0], submission.retry_after or 0.0)
        return StageOutcome(
            status=JobStatus.SUCCEEDED,
            operation=_operation(
                layout, submission.operation_url, submitted_at, submission.request_id
            ),
            next_jobs=(
                unit_job(
                    services.planner,
                    StageName.DI_POLL,
                    poll,
                    not_before=submitted_at + timedelta(seconds=delay),
                ),
            ),
            checkpoint={"operation_url": submission.operation_url, "billable_pages": billable},
        )


@dataclass(frozen=True)
class DiPollStage:
    settings: DiSettings = DiSettings()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(DiPollInputs, job)
        services = context.services
        layout = services.require_layout(inputs.account)
        operation = _operation(layout, inputs.operation_url, inputs.submitted_at)
        polls = int(job.checkpoint.get("polls", 0)) + 1
        async with scratch(services) as work:
            destination = work / "result.json"
            try:
                poll = await layout.poll(inputs.operation_url, destination)
            except RemoteResultMissingError as error:
                return StageOutcome(
                    status=JobStatus.FAILED_PERMANENT,
                    reason=f"{error}; replay di_submit for this unit",
                    error_category=error.category,
                )
            now = services.clock.now()
            if poll.status in (LayoutStatus.NOT_STARTED, LayoutStatus.RUNNING):
                elapsed = (now - inputs.submitted_at).total_seconds()
                if elapsed > self.settings.poll_deadline_seconds:
                    return StageOutcome(
                        status=JobStatus.FAILED_PERMANENT,
                        reason=f"DI operation still {poll.status.value} after {int(elapsed)}s",
                        error_category=ErrorCategory.TIMEOUT,
                    )
                schedule = self.settings.poll_schedule
                delay = max(schedule[min(polls - 1, len(schedule) - 1)], poll.retry_after or 0.0)
                return StageOutcome.awaiting_remote(
                    next_poll_at=now + timedelta(seconds=delay),
                    operation=operation,
                    checkpoint={"polls": polls, "status": poll.status.value},
                )
            if poll.status is not LayoutStatus.SUCCEEDED:
                return StageOutcome(
                    status=JobStatus.FAILED_PERMANENT,
                    reason=f"DI analysis {poll.status.value}: {poll.error or 'no detail'}",
                    error_category=ErrorCategory.INVALID_INPUT,
                )
            raw = await services.require_artifacts().put_file(
                destination, media_type=JSON_MEDIA_TYPE, schema_name=RAW_SCHEMA
            )
        services.faults.hit(FaultPoint.AFTER_ARTIFACT_WRITE)
        common = inputs.model_dump(
            mode="json", include={"plan_id", "content_hash", "family", "file_kind", "unit"}
        )
        successors = [
            unit_job(
                services.planner,
                StageName.NORMALIZE,
                NormalizeInputs.model_validate(
                    {**common, "raw": raw, "request": inputs.request, "page_map": inputs.page_map}
                ),
            )
        ]
        if inputs.request.get("figures"):
            figures = FigureInputs(
                unit_id=inputs.unit.unit_id,
                content_hash=inputs.content_hash,
                operation_url=inputs.operation_url,
                submitted_at=inputs.submitted_at,
                raw=raw,
                account=inputs.account,
            )
            successors.append(
                services.planner.spec(
                    StageName.FIGURE_ASSETS,
                    unit_id=f"{inputs.unit.unit_id}#figures",
                    inputs=figures.model_dump(mode="json"),
                    content_hash=inputs.content_hash,
                )
            )
        prices = self.settings.spend.prices
        return StageOutcome(
            status=JobStatus.SUCCEEDED,
            artifacts=(ArtifactLink(role="raw_layout", artifact=raw),),
            next_jobs=tuple(successors),
            operation=operation,
            usage=ProviderUsage(
                provider=PROVIDER,
                pages=inputs.billable_pages,
                requests=polls + 1,
                details={
                    "model_id": inputs.request.get("model_id"),
                    "api_version": layout.api_version,
                },
                account=inputs.account,
                cost_usd=None if prices is None else prices.di_cost(inputs.billable_pages),
            ),
            checkpoint={"polls": polls, "result_bytes": poll.result_bytes},
        )


def figure_ids(path: Path) -> list[str]:
    """Figure IDs (``page.index``) of a stored DI or CU response, in document order."""

    with path.open("rb") as handle:
        document = json.load(handle)
    if "analyzeResult" in document:
        figures = (document.get("analyzeResult") or {}).get("figures") or []
    else:
        contents = (document.get("result") or {}).get("contents") or []
        figures = [item for content in contents for item in content.get("figures") or []]
    return [str(figure["id"]) for figure in figures if figure.get("id")]


class _RemoteFiles(Protocol):
    async def fetch(self, figure_id: str, destination: Path) -> int: ...

    async def delete(self) -> None: ...


@dataclass(frozen=True)
class _LayoutFiles:
    layout: LayoutProvider
    operation_url: str

    async def fetch(self, figure_id: str, destination: Path) -> int:
        return await self.layout.figure(self.operation_url, figure_id, destination)

    async def delete(self) -> None:
        await self.layout.delete_result(self.operation_url)


@dataclass(frozen=True)
class _AnalysisFiles:
    analysis: ContentAnalysisProvider
    operation_url: str
    path_template: str

    async def fetch(self, figure_id: str, destination: Path) -> int:
        path = self.path_template.format(id=figure_id)
        return await self.analysis.file(self.operation_url, path, destination)

    async def delete(self) -> None:
        await self.analysis.delete_result(self.operation_url)


@dataclass(frozen=True)
class FigureAssetsStage:
    settings: DiSettings = DiSettings()
    cu_figure_path: str = "figure-{id}"
    """Result-file path of a CU figure crop (documented example: ``figure-1.1``)."""

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(FigureInputs, job)
        services = context.services
        remote: _RemoteFiles
        if inputs.provider == cu.PROVIDER:
            remote = _AnalysisFiles(
                services.require_content_analysis(inputs.account),
                inputs.operation_url,
                self.cu_figure_path,
            )
        else:
            remote = _LayoutFiles(services.require_layout(inputs.account), inputs.operation_url)
        repository = services.require_evidence()
        artifacts = services.require_artifacts()
        known = {asset.figure_id for asset in await repository.figures([inputs.unit_id])}
        links: list[ArtifactLink] = []
        stored = failed = expired = 0
        ids: list[str] = []
        async with scratch(services) as work:
            if inputs.capture:
                raw_path = work / "result.json"
                await artifacts.download_to(inputs.raw, raw_path)
                ids = await asyncio.to_thread(figure_ids, raw_path)
            pending = [figure for figure in ids if figure not in known]
            for position, figure in enumerate(pending):
                now = services.clock.now()
                age = (now - inputs.submitted_at).total_seconds()
                if age > self.settings.result_retention_seconds:
                    rest = pending[position:]
                    await _record(
                        repository, inputs.unit_id, rest, FigureState.EXPIRED,
                        "result retention passed", now,
                    )
                    expired += len(rest)
                    break
                destination = work / f"figure-{position}.png"
                try:
                    await remote.fetch(figure, destination)
                except RemoteResultMissingError as error:
                    rest = pending[position:]
                    await _record(
                        repository, inputs.unit_id, rest, FigureState.EXPIRED, str(error), now
                    )
                    expired += len(rest)
                    break
                except InvalidInputError as error:
                    await _record(
                        repository, inputs.unit_id, [figure], FigureState.FAILED, str(error), now
                    )
                    failed += 1
                    continue
                artifact = await artifacts.put_file(destination, media_type="image/png")
                await repository.record_figures(
                    [
                        FigureAsset(
                            unit_id=inputs.unit_id,
                            figure_id=figure,
                            state=FigureState.STORED,
                            artifact=artifact,
                        )
                    ],
                    now=services.clock.now(),
                )
                links.append(ArtifactLink(role="figure", artifact=artifact))
                stored += 1
        cleaned = True
        try:
            await remote.delete()
        except PipelineError:
            cleaned = False
        return StageOutcome.succeeded(
            artifacts=tuple(links),
            checkpoint={
                "figures": len(ids),
                "stored": stored,
                "failed": failed,
                "expired": expired,
                "result_deleted": cleaned,
            },
        )


async def _record(
    repository: EvidenceRepository,
    unit_id: str,
    figure_ids: list[str],
    state: FigureState,
    reason: str,
    now: datetime,
) -> None:
    await repository.record_figures(
        [
            FigureAsset(unit_id=unit_id, figure_id=figure, state=state, reason=reason[:500])
            for figure in figure_ids
        ],
        now=now,
    )
