"""Native extraction (no paid calls) and DI/CU result normalization into evidence units."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial

from ..domain import cu
from ..domain.cu import AnalyzerProfile
from ..domain.errors import IntegrityError, UnsupportedContentError
from ..domain.evidence import EvidenceUnit
from ..domain.models import ArtifactLink, StageJob, StageOutcome, UnitDisposition, UnitResult
from ..domain.quality import UnitQuality, failed_rule, measure_layout
from ..domain.routing import EscalationRule, Route
from ..domain.states import JobStatus
from .base import StageContext, StageServices
from .common import fetch_content, parse_inputs, put_json, scratch
from .cu import AnalysisOptions, cu_job
from .di import NormalizeInputs
from .links import UnitInputs
from .normalizers.cu import normalize_analysis
from .normalizers.di_layout import normalize_layout
from .normalizers.dwg import normalize_dwg
from .normalizers.ifc import normalize_ifc, read_ifc
from .normalizers.native import normalize_native_pdf, normalize_spreadsheet, normalize_text_file

STAGE_VERSION = "2"


@dataclass(frozen=True)
class ParserSettings:
    max_source_bytes: int = 500 * 1024 * 1024
    max_text_bytes: int = 64 * 1024 * 1024
    include_hidden_sheets: bool = True
    max_cells: int = 2_000_000


def _no_evidence(reason: str) -> StageOutcome:
    return StageOutcome(
        status=JobStatus.NO_EVIDENCE,
        reason=reason,
        unit_result=UnitResult(disposition=UnitDisposition.NO_EVIDENCE, reason=reason),
        checkpoint={"blocks": 0},
    )


async def _settle(
    services: StageServices, evidence: EvidenceUnit, empty_reason: str
) -> StageOutcome:
    """Store the unit's evidence; a unit without content settles as ``no_evidence``."""

    if not evidence.has_content:
        return _no_evidence(empty_reason)
    artifact = await put_json(services, evidence, schema_name=evidence.schema_name)
    return StageOutcome.succeeded(
        artifacts=(ArtifactLink(role="evidence_unit", artifact=artifact),),
        unit_result=UnitResult(disposition=UnitDisposition.EVIDENCE, evidence=artifact),
        checkpoint={"blocks": len(evidence.blocks), "pages": len(evidence.pages)},
    )


@dataclass(frozen=True)
class NativeParseStage:
    settings: ParserSettings = ParserSettings()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(UnitInputs, job)
        services = context.services
        unit = inputs.unit
        route = Route(unit.route)
        plan_id, content_hash = inputs.plan_id, inputs.content_hash
        async with scratch(services) as work:
            source = work / "source"
            limit = (
                self.settings.max_text_bytes
                if route is Route.NATIVE_TEXT
                else self.settings.max_source_bytes
            )
            await fetch_content(services, inputs.content_hash, source, max_bytes=limit)
            if route is Route.NATIVE_PDF_TEXT:
                texts = await services.require_pdf().page_texts(source, unit.pages)
                evidence = await asyncio.to_thread(
                    partial(
                        normalize_native_pdf,
                        texts,
                        unit=unit,
                        plan_id=plan_id,
                        content_hash=content_hash,
                    )
                )
                return await _settle(services, evidence, "no native text on these pages")
            if route is Route.NATIVE_SPREADSHEET:
                sheets = await services.require_sheets().read(
                    source,
                    include_hidden=self.settings.include_hidden_sheets,
                    max_cells=self.settings.max_cells,
                )
                evidence = await asyncio.to_thread(
                    partial(
                        normalize_spreadsheet,
                        sheets,
                        unit=unit,
                        plan_id=plan_id,
                        content_hash=content_hash,
                        independent_column_header_row=(
                            int(unit.locator["column_header_row"])
                            if unit.locator.get("spreadsheet_mode") == "independent_columns"
                            else None
                        ),
                    )
                )
                return await _settle(services, evidence, "workbook has no non-empty cells")
            if route is Route.NATIVE_TEXT:
                data = await asyncio.to_thread(source.read_bytes)
                evidence = await asyncio.to_thread(
                    partial(
                        normalize_text_file,
                        data,
                        text_format=str(unit.locator.get("format", "text")),
                        max_cells=self.settings.max_cells,
                        unit=unit,
                        plan_id=plan_id,
                        content_hash=content_hash,
                    )
                )
                return await _settle(services, evidence, "text file is empty")
            if route is Route.NATIVE_IFC:
                summary = await asyncio.to_thread(read_ifc, source)
                evidence = normalize_ifc(
                    summary, unit=unit, plan_id=plan_id, content_hash=content_hash
                )
                return await _settle(services, evidence, "IFC model has no identity data")
            if route is Route.NATIVE_DWG:
                try:
                    drawing = await services.require_drawings().read(source, work)
                except UnsupportedContentError as error:
                    # About 3% of the corpus: the file stays in lineage, without a review task.
                    return _no_evidence(f"lineage only: {error}")
                evidence = await asyncio.to_thread(
                    partial(
                        normalize_dwg,
                        drawing,
                        unit=unit,
                        plan_id=plan_id,
                        content_hash=content_hash,
                    )
                )
                return await _settle(services, evidence, "lineage only: DWG has no text")
        raise UnsupportedContentError(f"Route {route.value} is not a native route")


@dataclass(frozen=True)
class NormalizeStage:
    """Normalize a stored DI or CU result. Pure CPU work on an immutable artifact: replayable.

    A DI unit planned with an escalation rule that fails the quality gate is not settled here:
    its DI evidence is kept as an artifact and a CU job takes over the unit.
    """

    max_result_bytes: int = 512 * 1024 * 1024
    analysis: AnalysisOptions = field(default_factory=AnalysisOptions)

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(NormalizeInputs, job)
        services = context.services
        if inputs.raw.size_bytes > self.max_result_bytes:
            raise UnsupportedContentError(
                f"Analysis result of {inputs.raw.size_bytes} bytes exceeds {self.max_result_bytes}"
            )
        page_map = None
        if inputs.page_map is not None:
            try:
                page_map = {int(key): value for key, value in inputs.page_map.items()}
            except ValueError as error:
                raise IntegrityError("Page map keys must be page numbers") from error
        escalation = inputs.unit.locator.get("escalate") if inputs.provider != cu.PROVIDER else None
        rule = EscalationRule.model_validate(escalation) if escalation else None
        async with scratch(services) as work:
            path = work / "result.json"
            await services.require_artifacts().download_to(inputs.raw, path)

            def build() -> tuple[EvidenceUnit, UnitQuality | None]:
                with path.open("rb") as handle:
                    document = json.load(handle)
                if inputs.provider == cu.PROVIDER:
                    profile = AnalyzerProfile.model_validate(inputs.profile or {})
                    return normalize_analysis(
                        document,
                        unit=inputs.unit,
                        plan_id=inputs.plan_id,
                        content_hash=inputs.content_hash,
                        raw=inputs.raw,
                        page_map=page_map,
                        request=inputs.request,
                        profile=profile.identity(),
                        field_methods=profile.field_methods,
                    ), None
                evidence = normalize_layout(
                    document,
                    unit=inputs.unit,
                    plan_id=inputs.plan_id,
                    content_hash=inputs.content_hash,
                    raw=inputs.raw,
                    page_map=page_map,
                    request=inputs.request,
                )
                quality = measure_layout(document.get("analyzeResult") or {}) if rule else None
                return evidence, quality

            evidence, quality = await asyncio.to_thread(build)
        if rule is not None and quality is not None:
            failed = failed_rule(quality, inputs.unit.kind, [rule])
            if failed is not None:
                return await self._escalate(services, inputs, evidence, quality, failed)
        return await _settle(services, evidence, "analysis found no content")

    async def _escalate(
        self,
        services: StageServices,
        inputs: NormalizeInputs,
        evidence: EvidenceUnit,
        quality: UnitQuality,
        rule: EscalationRule,
    ) -> StageOutcome:
        artifact = await put_json(services, evidence, schema_name=evidence.schema_name)
        successor = cu_job(
            services.planner, inputs, rule.analyzer_id, services.require_analyzers(), self.analysis
        )
        return StageOutcome.succeeded(
            artifacts=(ArtifactLink(role="escalated_evidence", artifact=artifact),),
            next_jobs=(successor,),
            checkpoint={
                "escalated_to": rule.analyzer_id,
                "words_per_page": round(quality.words_per_page, 2),
                "mean_word_confidence": quality.mean_word_confidence,
            },
        )
