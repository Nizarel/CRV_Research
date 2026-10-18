"""Plan: split content into bounded units and register them with their jobs atomically.

The plan, its units, every extraction job and the blocked assembly job (released by the plan
barrier when all units settle) commit in one ledger transaction: there is no in-memory fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.errors import UnsupportedContentError
from ..domain.models import (
    ArtifactLink,
    JobSpec,
    PlanRecord,
    StageJob,
    StageOutcome,
    UnitDisposition,
    UnitRecord,
)
from ..domain.routing import (
    DI_ROUTES,
    NATIVE_ROUTES,
    PreflightReport,
    Route,
    SubmitMode,
    UnitDraft,
    plan_id_for,
    plan_units,
    unit_id_for,
)
from ..domain.states import StageName
from ..ports.providers import LayoutRequest
from .base import StageContext, StageServices
from .chunk import AssembleInputs
from .common import fetch_content, load_model, page_ranges, parse_inputs, scratch
from .cu import AnalysisOptions, cu_job
from .di import DiInputs
from .links import PlanInputs, UnitInputs, unit_job

STAGE_VERSION = "2"


@dataclass(frozen=True)
class LayoutOptions:
    """Document Intelligence options; every one of them is part of the job fingerprint."""

    model_id: str = "prebuilt-layout"
    output_content_format: str = "markdown"
    figures: bool = True
    features: tuple[str, ...] = ()
    drawing_features: tuple[str, ...] = ()
    locale: str | None = None
    string_index_type: str = "unicodeCodePoint"

    def request(self, unit: UnitRecord, mode: SubmitMode) -> LayoutRequest:
        features = self.drawing_features if unit.route == Route.DI_LAYOUT_DRAWING else self.features
        return LayoutRequest(
            model_id=self.model_id,
            pages=page_ranges(unit.pages) if mode is SubmitMode.PAGES and unit.pages else None,
            output_content_format=self.output_content_format,
            string_index_type=self.string_index_type,
            figures=self.figures,
            features=tuple(sorted(set(features))),
            locale=self.locale,
        )


@dataclass(frozen=True)
class PlanStage:
    layout: LayoutOptions = field(default_factory=LayoutOptions)
    analysis: AnalysisOptions = field(default_factory=AnalysisOptions)
    max_source_bytes: int = 1024**3

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(PlanInputs, job)
        services = context.services
        report = await load_model(services, inputs.preflight, PreflightReport)
        policy = inputs.policy
        plan_id = plan_id_for(report, policy)
        drafts = plan_units(report, policy)
        drafts = await self._bounded_units(
            services, report, drafts, min(policy.max_submit_bytes, policy.di_max_bytes)
        )
        units = tuple(
            UnitRecord(
                unit_id=unit_id_for(plan_id, ordinal),
                ordinal=ordinal,
                kind=draft.kind.value,
                route=draft.route.value,
                pages=draft.pages,
                locator=draft.locator,
                signals=draft.signals,
                disposition=draft.disposition,
                reason=draft.reason,
            )
            for ordinal, draft in enumerate(drafts)
        )
        plan = PlanRecord(
            plan_id=plan_id,
            content_hash=report.content_hash,
            snapshot_id=report.snapshot_id,
            policy_version=policy.version,
            family=report.family.value,
            preflight_sha256=inputs.preflight.sha256,
            page_count=report.page_count,
            units=units,
        )
        jobs = [
            self._extraction_job(services, plan, report, unit)
            for unit in units
            if unit.disposition is None
        ]
        if jobs:
            assemble = AssembleInputs(
                plan_id=plan_id,
                content_hash=report.content_hash,
                policy_version=policy.version,
                family=report.family.value,
                page_count=report.page_count,
            )
            jobs.append(
                services.planner.spec(
                    StageName.CHUNK,
                    unit_id=plan_id,
                    inputs=assemble.model_dump(mode="json", exclude_none=True),
                    content_hash=report.content_hash,
                    barrier_plan_id=plan_id,
                )
            )
        return StageOutcome.succeeded(
            plan=plan,
            next_jobs=tuple(jobs),
            artifacts=(ArtifactLink(role="preflight", artifact=inputs.preflight),),
            checkpoint={
                "plan_id": plan_id,
                "units": len(units),
                "extraction_jobs": len(jobs) - (1 if jobs else 0),
            },
        )

    async def _bounded_units(
        self, services: StageServices, report: PreflightReport,
        drafts: list[UnitDraft], limit: int,
    ) -> list[UnitDraft]:
        if not any(d.locator.get("submit") == SubmitMode.SUBSET.value for d in drafts):
            return drafts
        async with scratch(services) as work:
            source, subset = work / "source", work / "subset.pdf"
            await fetch_content(
                services, report.content_hash, source, max_bytes=self.max_source_bytes
            )
            result: list[UnitDraft] = []

            async def split(draft: UnitDraft) -> None:
                if draft.disposition or draft.locator.get("submit") != SubmitMode.SUBSET.value:
                    result.append(draft)
                    return
                try:
                    await services.require_pdf().write_subset(source, draft.pages, subset)
                    size = subset.stat().st_size
                except UnsupportedContentError as error:
                    result.append(draft.model_copy(update={
                        "disposition": UnitDisposition.UNSUPPORTED, "reason": str(error),
                    }))
                    return
                finally:
                    subset.unlink(missing_ok=True)
                if size <= limit:
                    result.append(draft.model_copy(update={
                        "locator": {**draft.locator, "max_submit_bytes": limit}
                    }))
                elif len(draft.pages) == 1:
                    result.append(draft.model_copy(update={
                        "disposition": UnitDisposition.UNSUPPORTED,
                        "reason": f"Single-page PDF is {size} bytes; submit limit is {limit}",
                    }))
                else:
                    middle = len(draft.pages) // 2
                    for pages in (draft.pages[:middle], draft.pages[middle:]):
                        await split(draft.model_copy(update={"pages": pages}))

            for draft in drafts:
                await split(draft)
        return result

    def _extraction_job(
        self, services: StageServices, plan: PlanRecord, report: PreflightReport, unit: UnitRecord
    ) -> JobSpec:
        planner = services.planner
        route = Route(unit.route)
        common = {
            "plan_id": plan.plan_id,
            "content_hash": plan.content_hash,
            "family": plan.family,
            "file_kind": report.file_kind.value,
            "unit": unit,
        }
        if route in NATIVE_ROUTES:
            return unit_job(planner, StageName.NATIVE_PARSE, UnitInputs(**common))
        if route is Route.CU_ANALYZE:
            return cu_job(
                planner,
                UnitInputs(**common),
                str(unit.locator["profile"]),
                services.require_analyzers(),
                self.analysis,
            )
        if route not in DI_ROUTES:
            raise ValueError(f"Route {route.value} has no extraction stage")
        mode = SubmitMode(unit.locator.get("submit", SubmitMode.WHOLE.value))
        request = self.layout.request(unit, mode)
        return unit_job(
            planner,
            StageName.DI_SUBMIT,
            DiInputs(**common, request=request.identity(), submit_mode=mode),
        )
