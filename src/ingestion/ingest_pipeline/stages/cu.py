"""Content Understanding stages: budgeted submit, durable polling and result reuse.

Remote-operation discipline is the same as for Document Intelligence: the Operation-Location is
recorded in the ledger before polling, polls are rescheduled jobs and results are streamed to
disk then stored immutably. Two additions: admission checks a spend cap on pages *and* model
tokens (in-flight work counted at an estimate), and a stored result is reused by any routing
policy that asks for the same analysis (same content, pages, request and analyzer profile).
Workers only read analyzers; they never create or change them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime

from ..domain.cu import MIME_TYPES, PROVIDER, RAW_SCHEMA, AnalyzerProfile, usage_from
from ..domain.errors import ErrorCategory, RemoteResultMissingError, UnsupportedContentError
from ..domain.ids import fingerprint
from ..domain.models import (
    ArtifactLink,
    ArtifactRef,
    JobSpec,
    OperationRef,
    ProviderUsage,
    StageJob,
    StageOutcome,
    UnitRecord,
)
from ..domain.planning import JobPlanner
from ..domain.routing import SubmitMode
from ..domain.sniff import FileKind
from ..domain.states import JobStatus, StageName
from ..faults import FaultPoint
from ..ports.evidence import CachedExtraction
from ..ports.providers import (
    AnalysisRequest,
    AnalysisStatus,
    AnalyzerCatalog,
    ContentAnalysisProvider,
)
from .base import StageContext, StageServices
from .common import JSON_MEDIA_TYPE, fetch_content, page_ranges, parse_inputs, scratch
from .di import FigureInputs, NormalizeInputs, parked, throttled
from .links import UnitInputs, unit_job
from .spend import SpendPolicy, start_on_account

STAGE_VERSION = "1"


class CuInputs(UnitInputs):
    request: dict[str, Any]
    profile: dict[str, Any]
    """``AnalyzerProfile`` resolved from the verified catalog when the job was planned."""
    submit_mode: SubmitMode
    resubmit: int | None = None
    """Resubmission number after a transient service failure; unset on the first submit."""


class CuPollInputs(CuInputs):
    operation_url: str
    submitted_at: AwareDatetime
    billable_pages: int
    extraction: str
    """Extraction-cache fingerprint of this analysis."""
    page_map: dict[str, int] | None = None
    account: str | None = None
    """Billing account that accepted the operation; ``None`` is this subscription."""


@dataclass(frozen=True)
class AnalysisOptions:
    """Request options shared by every CU job; all of them are part of the fingerprint."""

    processing_location: str = "dataZone"
    string_encoding: str = "codePoint"

    def request(
        self, unit: UnitRecord, mode: SubmitMode, analyzer_id: str, file_kind: str
    ) -> AnalysisRequest:
        mime = (
            MIME_TYPES[FileKind.PDF]
            if mode is SubmitMode.SUBSET
            else MIME_TYPES.get(FileKind(file_kind), "application/octet-stream")
        )
        return AnalysisRequest(
            analyzer_id=analyzer_id,
            content_range=(
                page_ranges(unit.pages) if mode is SubmitMode.PAGES and unit.pages else None
            ),
            mime_type=mime,
            processing_location=self.processing_location,
            string_encoding=self.string_encoding,
        )


@dataclass(frozen=True)
class CuSettings:
    max_source_bytes: int = 200 * 1024 * 1024
    max_inflight: int = 4
    page_budget: int | None = None
    """Hard cap on content-extraction pages; ``None`` means no cap."""
    token_budget: int | None = None
    """Hard cap on completion-model input plus output tokens; ``None`` means no cap."""
    est_tokens_per_page: int = 12_000
    """Reserved per in-flight page until the operation reports its real usage."""
    admission_wait_seconds: float = 30.0
    park_seconds: float = 1800.0
    """Safety timer for a submit parked on the in-flight limit; a freed slot wakes it sooner."""
    poll_schedule: tuple[float, ...] = (2.0, 5.0, 13.0, 34.0, 60.0)
    poll_deadline_seconds: float = 3600.0
    capture_figures: bool = False
    """Store figure crops; off until the result-file path is verified against the service."""
    max_resubmits: int = 2
    resubmit_backoff_seconds: float = 120.0
    spend: SpendPolicy = field(default_factory=SpendPolicy)


# The service reports model throttling inside an analysis as a failed operation.
_TRANSIENT_FAILURES = (
    "ResourceError",
    "TooManyRequests",
    "ServiceUnavailable",
    "InternalServerError",
)


def transient_failure(error: str | None) -> bool:
    return any(marker in (error or "") for marker in _TRANSIENT_FAILURES)


def _jittered(seconds: float, job_id: str) -> float:
    """Spread waiting submits so they do not all retry admission at the same moment."""

    return seconds * (1.0 + int(fingerprint("jitter", {"job": job_id})[:4], 16) / 0x10000 / 2)


def cu_job(
    planner: JobPlanner,
    inputs: UnitInputs,
    analyzer_id: str,
    catalog: AnalyzerCatalog,
    options: AnalysisOptions,
) -> JobSpec:
    """Plan a CU submit for a unit; the analyzer must be in the verified catalog."""

    unit = inputs.unit
    mode = SubmitMode(unit.locator.get("submit", SubmitMode.WHOLE.value))
    profile = catalog.profile(analyzer_id)
    request = options.request(unit, mode, analyzer_id, inputs.file_kind)
    return unit_job(
        planner,
        StageName.CU_SUBMIT,
        CuInputs(
            **inputs.model_dump(include={"plan_id", "content_hash", "family", "file_kind"}),
            unit=unit,
            request=request.identity(),
            profile=profile.model_dump(mode="json"),
            submit_mode=mode,
        ),
    )


def extraction_key(inputs: CuInputs, api_version: str) -> str:
    profile = AnalyzerProfile.model_validate(inputs.profile)
    return fingerprint(
        "cu-extraction",
        {
            "provider": PROVIDER,
            "api_version": api_version,
            "content_hash": inputs.content_hash,
            "pages": list(inputs.unit.pages),
            "submit_mode": inputs.submit_mode.value,
            "request": inputs.request,
            "profile": profile.identity(),
        },
    )


def _normalize_job(
    planner: JobPlanner, inputs: CuInputs, raw: ArtifactRef, page_map: dict[str, int] | None
) -> JobSpec:
    common = inputs.model_dump(
        mode="json", include={"plan_id", "content_hash", "family", "file_kind", "unit"}
    )
    return unit_job(
        planner,
        StageName.NORMALIZE,
        NormalizeInputs.model_validate(
            {
                **common,
                "raw": raw,
                "request": inputs.request,
                "page_map": page_map,
                "provider": PROVIDER,
                "profile": inputs.profile,
            }
        ),
    )


def _operation(
    provider: ContentAnalysisProvider,
    url: str,
    submitted_at: datetime,
    request_id: str | None = None,
) -> OperationRef:
    return OperationRef(
        provider=PROVIDER,
        operation_url=url,
        api_version=provider.api_version,
        submitted_at=submitted_at,
        request_id=request_id,
    )


@dataclass(frozen=True)
class CuSubmitStage:
    settings: CuSettings = CuSettings()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(CuInputs, job)
        services = context.services
        provider = services.require_content_analysis()
        key = extraction_key(inputs, provider.api_version)
        if services.extraction_cache is not None:
            hit = await services.extraction_cache.cached_extraction(key)
            if hit is not None:
                return StageOutcome.succeeded(
                    next_jobs=(_normalize_job(services.planner, inputs, hit.raw, hit.page_map),),
                    checkpoint={"reused": key},
                )
        billable = max(len(inputs.unit.pages), 1)
        analyzer_id = str(inputs.request["analyzer_id"])
        now = services.clock.now()
        choices = await self.settings.spend.candidates(
            services.require_workload(),
            provider=PROVIDER,
            pages=billable,
            page_usd=lambda prices: prices.cu_page_estimate_usd,
            serves=lambda account: services.accepts_cu_work(account, analyzer_id),
        )
        if choices.blocked is not None:
            return throttled(
                now,
                _jittered(self.settings.admission_wait_seconds * 10, job.job_id),
                choices.blocked,
            )
        admitted, account = await start_on_account(
            context,
            choices.accounts,
            provider=PROVIDER,
            polls=StageName.CU_POLL,
            default_limit=self.settings.max_inflight,
        )
        if not admitted:
            return parked(
                now,
                _jittered(self.settings.park_seconds, job.job_id),
                "CU in-flight limit reached",
            )
        blocked = await self._caps(context, job.job_id, billable=billable)
        if blocked is not None:
            return blocked
        provider = services.require_content_analysis(account)
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
                raise UnsupportedContentError("Serialized CU input exceeds its planned byte limit")
            submission = await provider.submit(submit_path, _request(inputs.request))
        services.faults.hit(FaultPoint.AFTER_REMOTE_ACCEPT)
        submitted_at = services.clock.now()
        poll = CuPollInputs.model_validate(
            {
                **inputs.model_dump(mode="json"),
                "operation_url": submission.operation_url,
                "submitted_at": submitted_at,
                "billable_pages": billable,
                "extraction": key,
                "page_map": page_map,
                "account": account,
            }
        )
        delay = max(self.settings.poll_schedule[0], submission.retry_after or 0.0)
        return StageOutcome(
            status=JobStatus.SUCCEEDED,
            operation=_operation(
                provider, submission.operation_url, submitted_at, submission.request_id
            ),
            next_jobs=(
                unit_job(
                    services.planner,
                    StageName.CU_POLL,
                    poll,
                    not_before=submitted_at + timedelta(seconds=delay),
                ),
            ),
            checkpoint={"operation_url": submission.operation_url, "billable_pages": billable},
        )

    async def _caps(
        self, context: StageContext, job_id: str, *, billable: int
    ) -> StageOutcome | None:
        settings = self.settings
        workload = context.services.require_workload()
        now = context.services.clock.now()
        if settings.page_budget is None and settings.token_budget is None:
            return None
        used = await workload.provider_usage(PROVIDER)
        polls = await workload.active_work(StageName.CU_POLL)
        running = (await workload.active_work(StageName.CU_SUBMIT)).running
        # Other admitted submits still uploading: one page each, this job excluded.
        in_flight = polls.billable_pages + max(running - 1, 0)
        reserved = in_flight + billable
        if settings.page_budget is not None and used.pages + reserved > settings.page_budget:
            return throttled(
                now,
                _jittered(settings.admission_wait_seconds * 10, job_id),
                f"CU page budget {settings.page_budget} reached "
                f"({used.pages} used, {in_flight} in flight)",
            )
        tokens = used.input_tokens + used.output_tokens
        estimate = reserved * settings.est_tokens_per_page
        if settings.token_budget is not None and tokens + estimate > settings.token_budget:
            return throttled(
                now,
                _jittered(settings.admission_wait_seconds * 10, job_id),
                f"CU token budget {settings.token_budget} reached "
                f"({tokens} used, ~{estimate} reserved)",
            )
        return None


def _request(identity: dict[str, Any]) -> AnalysisRequest:
    return AnalysisRequest(
        analyzer_id=str(identity["analyzer_id"]),
        content_range=identity.get("content_range"),
        mime_type=str(identity["mime_type"]),
        processing_location=str(identity["processing_location"]),
        string_encoding=str(identity["string_encoding"]),
    )


def _usage(path: Path, *, requests: int, billable: int) -> ProviderUsage:
    with path.open("rb") as handle:
        document = json.load(handle)
    usage = usage_from(document.get("usage"), requests=requests)
    if usage.pages == 0:
        # A result without a usage block is still billed: count the submitted pages.
        usage = usage.model_copy(update={"pages": billable})
    return usage


@dataclass(frozen=True)
class CuPollStage:
    settings: CuSettings = CuSettings()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(CuPollInputs, job)
        services = context.services
        provider = services.require_content_analysis(inputs.account)
        operation = _operation(provider, inputs.operation_url, inputs.submitted_at)
        polls = int(job.checkpoint.get("polls", 0)) + 1
        async with scratch(services) as work:
            destination = work / "result.json"
            try:
                poll = await provider.poll(inputs.operation_url, destination)
            except RemoteResultMissingError as error:
                return StageOutcome(
                    status=JobStatus.FAILED_PERMANENT,
                    reason=f"{error}; replay cu_submit for this unit",
                    error_category=error.category,
                )
            now = services.clock.now()
            if poll.status in (AnalysisStatus.NOT_STARTED, AnalysisStatus.RUNNING):
                elapsed = (now - inputs.submitted_at).total_seconds()
                if elapsed > self.settings.poll_deadline_seconds:
                    return StageOutcome(
                        status=JobStatus.FAILED_PERMANENT,
                        reason=f"CU operation still {poll.status.value} after {int(elapsed)}s",
                        error_category=ErrorCategory.TIMEOUT,
                    )
                schedule = self.settings.poll_schedule
                delay = max(schedule[min(polls - 1, len(schedule) - 1)], poll.retry_after or 0.0)
                return StageOutcome.awaiting_remote(
                    next_poll_at=now + timedelta(seconds=delay),
                    operation=operation,
                    checkpoint={"polls": polls, "status": poll.status.value},
                )
            if poll.status is not AnalysisStatus.SUCCEEDED:
                return self._failed(services, inputs, operation, poll.status, poll.error, polls)
            usage = await asyncio.to_thread(
                _usage, destination, requests=polls + 1, billable=inputs.billable_pages
            )
            usage = self._billed(usage, inputs.account)
            raw = await services.require_artifacts().put_file(
                destination, media_type=JSON_MEDIA_TYPE, schema_name=RAW_SCHEMA
            )
        services.faults.hit(FaultPoint.AFTER_ARTIFACT_WRITE)
        if services.extraction_cache is not None:
            await services.extraction_cache.cache_extraction(
                CachedExtraction(
                    fingerprint=inputs.extraction,
                    provider=PROVIDER,
                    content_hash=inputs.content_hash,
                    raw=raw,
                    page_map=inputs.page_map,
                    details={"usage": usage.model_dump(mode="json")},
                ),
                now=services.clock.now(),
            )
        cleanup = FigureInputs(
            unit_id=inputs.unit.unit_id,
            content_hash=inputs.content_hash,
            operation_url=inputs.operation_url,
            submitted_at=inputs.submitted_at,
            raw=raw,
            provider=PROVIDER,
            capture=self.settings.capture_figures,
            account=inputs.account,
        )
        return StageOutcome(
            status=JobStatus.SUCCEEDED,
            artifacts=(ArtifactLink(role="raw_analysis", artifact=raw),),
            next_jobs=(
                _normalize_job(services.planner, inputs, raw, inputs.page_map),
                services.planner.spec(
                    StageName.FIGURE_ASSETS,
                    unit_id=f"{inputs.unit.unit_id}#figures",
                    inputs=cleanup.model_dump(mode="json"),
                    content_hash=inputs.content_hash,
                ),
            ),
            operation=operation,
            usage=usage,
            checkpoint={"polls": polls, "result_bytes": poll.result_bytes},
        )

    def _failed(
        self,
        services: StageServices,
        inputs: CuPollInputs,
        operation: OperationRef,
        status: AnalysisStatus,
        error: str | None,
        polls: int,
    ) -> StageOutcome:
        """A failed analysis: resubmit a transient service failure, otherwise fail the unit."""

        reason = f"CU analysis {status.value}: {error or 'no detail'}"
        # Whether a failed analysis is billed is not documented: count it against the cap.
        usage = self._billed(
            ProviderUsage(
                provider=PROVIDER,
                pages=inputs.billable_pages,
                requests=polls + 1,
                details={"failed_analysis": True, "estimated": True},
            ),
            inputs.account,
        )
        attempt = (inputs.resubmit or 0) + 1
        if not transient_failure(error) or attempt > self.settings.max_resubmits:
            return StageOutcome(
                status=JobStatus.FAILED_PERMANENT,
                reason=reason,
                error_category=ErrorCategory.INVALID_INPUT,
                operation=operation,
                usage=usage,
            )
        fields = inputs.model_dump(mode="json", include=set(CuInputs.model_fields))
        retry = CuInputs.model_validate({**fields, "resubmit": attempt})
        delay = self.settings.resubmit_backoff_seconds * attempt
        return StageOutcome.succeeded(
            next_jobs=(
                unit_job(
                    services.planner,
                    StageName.CU_SUBMIT,
                    retry,
                    not_before=services.clock.now() + timedelta(seconds=delay),
                ),
            ),
            usage=usage,
            checkpoint={"polls": polls, "resubmitted": attempt, "reason": reason[:500]},
        )

    def _billed(self, usage: ProviderUsage, account: str | None) -> ProviderUsage:
        prices = self.settings.spend.prices
        update: dict[str, Any] = {"account": account}
        if prices is not None:
            cost, unpriced = prices.cu_cost(usage)
            update["cost_usd"] = cost
            if unpriced:
                update["details"] = {**usage.details, "unpriced": unpriced}
        return usage.model_copy(update=update)
