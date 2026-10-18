"""Content families, routes and deterministic unit planning (LLD §6, §6.1).

Planning is a pure function of the preflight report and the routing policy, so the same bytes
and policy always yield the same plan, units and job fingerprints.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .ids import Sha256Hex, fingerprint
from .models import UnitDisposition
from .sniff import FileKind

PREFLIGHT_SCHEMA = "preflight.v1"
MIB = 1024 * 1024


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Family(StrEnum):
    PDF = "pdf"
    SPREADSHEET = "spreadsheet"
    TEXT = "text"
    OFFICE = "office"
    IMAGE = "image"
    ARCHIVE = "archive"
    EXECUTABLE = "executable"
    EMPTY = "empty"
    BIM = "bim"
    CAD = "cad"
    UNKNOWN = "unknown"


FAMILY_BY_KIND: dict[FileKind, Family] = {
    FileKind.PDF: Family.PDF,
    FileKind.XLSX: Family.SPREADSHEET,
    FileKind.XLSB: Family.SPREADSHEET,
    FileKind.XLS: Family.SPREADSHEET,
    FileKind.ODS: Family.SPREADSHEET,
    FileKind.TEXT: Family.TEXT,
    FileKind.DOCX: Family.OFFICE,
    FileKind.PPTX: Family.OFFICE,
    FileKind.HTML: Family.OFFICE,
    FileKind.PNG: Family.IMAGE,
    FileKind.JPEG: Family.IMAGE,
    FileKind.TIFF: Family.IMAGE,
    FileKind.BMP: Family.IMAGE,
    FileKind.HEIF: Family.IMAGE,
    FileKind.ZIP: Family.ARCHIVE,
    FileKind.EXECUTABLE: Family.EXECUTABLE,
    FileKind.EMPTY: Family.EMPTY,
    FileKind.IFC: Family.BIM,
    FileKind.IFC_ZIP: Family.CAD,
    FileKind.STEP: Family.CAD,
    FileKind.IGES: Family.CAD,
    FileKind.DWG: Family.CAD,
    FileKind.DXF: Family.CAD,
    FileKind.NAVISWORKS: Family.CAD,
    FileKind.PLC_PROGRAM: Family.CAD,
    FileKind.STEEL_MODEL: Family.CAD,
}


class Route(StrEnum):
    NATIVE_PDF_TEXT = "native_pdf_text"
    NATIVE_SPREADSHEET = "native_spreadsheet"
    NATIVE_TEXT = "native_text"
    NATIVE_IFC = "native_ifc"
    """Identity attributes of an IFC model (project, site, tags, document numbers), no geometry."""
    NATIVE_DWG = "native_dwg"
    """Text and block attributes of a DWG, converted to DXF by an external process; no geometry."""
    DI_LAYOUT = "di_layout"
    DI_LAYOUT_DRAWING = "di_layout_drawing"
    """Provisional: drawings use DI Layout per sheet until Content Understanding is enabled."""
    DI_LAYOUT_OFFICE = "di_layout_office"
    CU_ANALYZE = "cu_analyze"
    """Content Understanding analyzer named by the unit locator's ``profile``."""
    BLANK = "blank"
    UNSUPPORTED = "unsupported"


DI_ROUTES = frozenset({Route.DI_LAYOUT, Route.DI_LAYOUT_DRAWING, Route.DI_LAYOUT_OFFICE})
NATIVE_ROUTES = frozenset(
    {
        Route.NATIVE_PDF_TEXT,
        Route.NATIVE_SPREADSHEET,
        Route.NATIVE_TEXT,
        Route.NATIVE_IFC,
        Route.NATIVE_DWG,
    }
)
ANALYZER_ID = r"^[A-Za-z0-9._-]{1,64}$"


class UnitKind(StrEnum):
    PDF_PAGES = "pdf_pages"
    DRAWING_SHEET = "drawing_sheet"
    BLANK_PAGES = "blank_pages"
    SPREADSHEET = "spreadsheet"
    TEXT_FILE = "text_file"
    OFFICE_DOCUMENT = "office_document"
    IMAGE = "image"
    BIM_MODEL = "bim_model"
    CAD_DRAWING = "cad_drawing"
    WHOLE_FILE = "whole_file"


class SubmitMode(StrEnum):
    PAGES = "pages"
    """Send the original file with DI's ``pages`` parameter; page numbers stay original."""
    SUBSET = "subset"
    """Send a sub-PDF of the unit's pages (source too large); a page map is recorded."""
    WHOLE = "whole"


class PageSignals(_Frozen):
    page: int = Field(ge=1)
    width_pt: float = Field(ge=0)
    height_pt: float = Field(ge=0)
    rotation: int = 0
    text_chars: int = Field(ge=0)
    """Non-whitespace characters in the native text layer."""
    garbage_ratio: float = Field(default=0.0, ge=0, le=1)
    image_objects: int = Field(default=0, ge=0)
    path_objects: int = Field(default=0, ge=0)
    label: str | None = None
    multi_column: bool = False


class SheetSignals(_Frozen):
    name: str
    index: int = Field(ge=0)
    visibility: str = "visible"
    rows: int = Field(default=0, ge=0)
    columns: int = Field(default=0, ge=0)


class PreflightReport(_Frozen):
    schema_name: str = PREFLIGHT_SCHEMA
    content_hash: Sha256Hex
    size_bytes: int = Field(ge=0)
    snapshot_id: str
    blob_name: str
    extension: str
    file_kind: FileKind
    declared_kind: FileKind | None = None
    family: Family
    page_count: int | None = Field(default=None, ge=0)
    pages: tuple[PageSignals, ...] = ()
    sheets: tuple[SheetSignals, ...] = ()
    folder_hint: str | None = None
    """First folder of the placement path: a weak prior, never authoritative."""
    notes: tuple[str, ...] = ()

    @property
    def mismatch(self) -> bool:
        return self.declared_kind is not None and self.declared_kind is not self.file_kind


class CuProfileRule(_Frozen):
    """Send DI-routed units of these kinds to a Content Understanding analyzer instead."""

    unit_kinds: frozenset[UnitKind]
    analyzer_id: str = Field(pattern=ANALYZER_ID)

    @field_serializer("unit_kinds")
    def _sorted_kinds(self, kinds: frozenset[UnitKind]) -> list[str]:
        return sorted(kind.value for kind in kinds)


class EscalationRule(_Frozen):
    """Selective extraction: a DI unit of these kinds failing the gate is re-run on CU."""

    unit_kinds: frozenset[UnitKind]
    analyzer_id: str = Field(pattern=ANALYZER_ID)
    min_words_per_page: int = Field(default=0, ge=0)
    min_word_confidence: float = Field(default=0.0, ge=0, le=1)
    identifier_pattern: str | None = None
    """Regex that must match the unit text (e.g. a printed drawing number)."""

    @field_serializer("unit_kinds")
    def _sorted_kinds(self, kinds: frozenset[UnitKind]) -> list[str]:
        return sorted(kind.value for kind in kinds)


class RoutingPolicy(_Frozen):
    version: str = "routing-v1"
    prefer_native_text: bool = True
    preserve_layout: bool = False
    """Prefer layout extraction for images, vector-rich pages and multiple text columns."""
    independent_column_workbooks: frozenset[Sha256Hex] = frozenset()
    """Reviewed content hashes whose spreadsheet columns are independent lists."""
    independent_column_header_row: int = Field(default=2, ge=1)
    min_text_chars: int = Field(default=80, ge=1)
    max_garbage_ratio: float = Field(default=0.2, ge=0, le=1)
    drawing_min_area_pt2: float = Field(default=950_000, gt=0)
    """Just under A3 (842 x 1191 pt): larger sheets are planned as drawings, one per unit."""
    max_native_pages_per_unit: int = Field(default=50, ge=1)
    max_layout_pages_per_unit: int = Field(default=20, ge=1)
    max_drawing_pages_per_unit: int = Field(default=1, ge=1, le=2)
    max_submit_bytes: int = Field(default=40 * MIB, gt=0)
    di_max_bytes: int = Field(default=500 * MIB, gt=0)
    di_max_pages: int = Field(default=2000, gt=0)
    enabled_routes: frozenset[Route] = frozenset(Route)
    cu_profiles: tuple[CuProfileRule, ...] = ()
    """The first rule matching a DI-routed unit's kind moves the unit to that analyzer."""
    cu_content_profiles: dict[Sha256Hex, str] = Field(default_factory=dict)
    """Explicit analyzer per content hash (experiment manifests); checked before the rules."""
    escalation: tuple[EscalationRule, ...] = ()

    @field_serializer("enabled_routes")
    def _sorted_routes(self, routes: frozenset[Route]) -> list[str]:
        return sorted(route.value for route in routes)

    @field_serializer("independent_column_workbooks")
    def _sorted_workbooks(self, values: frozenset[str]) -> list[str]:
        return sorted(values)


class UnitDraft(_Frozen):
    kind: UnitKind
    route: Route
    pages: tuple[int, ...] = ()
    locator: dict[str, Any] = Field(default_factory=dict)
    signals: dict[str, Any] = Field(default_factory=dict)
    disposition: UnitDisposition | None = None
    reason: str | None = None

    @property
    def extractable(self) -> bool:
        return self.disposition is None


PLACEMENT_FIELDS = frozenset(
    {"snapshot_id", "blob_name", "folder_hint", "extension", "declared_kind", "notes"}
)
"""Report fields that describe one placement, not the content; excluded from plan identity."""


def plan_id_for(report: PreflightReport, policy: RoutingPolicy) -> str:
    content = report.model_dump(mode="json", exclude=set(PLACEMENT_FIELDS))
    return "plan:" + fingerprint("plan", {"content": content, "policy": policy})


def unit_id_for(plan_id: str, ordinal: int) -> str:
    return f"unit:{plan_id.removeprefix('plan:')[:40]}:{ordinal:05d}"


def classify_page(signals: PageSignals, policy: RoutingPolicy) -> Route:
    if not (signals.text_chars or signals.image_objects or signals.path_objects):
        return Route.BLANK
    if signals.width_pt * signals.height_pt >= policy.drawing_min_area_pt2:
        return Route.DI_LAYOUT_DRAWING
    if policy.preserve_layout and (
        signals.multi_column or signals.image_objects or signals.path_objects >= 10
    ):
        return Route.DI_LAYOUT
    if (
        policy.prefer_native_text
        and signals.text_chars >= policy.min_text_chars
        and signals.garbage_ratio <= policy.max_garbage_ratio
    ):
        return Route.NATIVE_PDF_TEXT
    return Route.DI_LAYOUT


def plan_units(report: PreflightReport, policy: RoutingPolicy) -> list[UnitDraft]:
    """Split one source into bounded units that together cover every original page."""

    if report.family is Family.PDF:
        drafts = _pdf_units(report, policy)
    else:
        drafts = [_whole_file_unit(report, policy)]
    return [_apply_enablement(_assign_cu(draft, report, policy), policy) for draft in drafts]


def _assign_cu(draft: UnitDraft, report: PreflightReport, policy: RoutingPolicy) -> UnitDraft:
    if draft.disposition is not None or draft.route not in DI_ROUTES:
        return draft
    analyzer = policy.cu_content_profiles.get(report.content_hash) or next(
        (rule.analyzer_id for rule in policy.cu_profiles if draft.kind in rule.unit_kinds), None
    )
    if analyzer is None:
        rule = next((rule for rule in policy.escalation if draft.kind in rule.unit_kinds), None)
        if rule is None:
            return draft
        # The unit stays on DI; normalize re-runs it on CU when the quality gate fails.
        locator = {**draft.locator, "escalate": rule.model_dump(mode="json")}
        return draft.model_copy(update={"locator": locator})
    locator = {**draft.locator, "profile": analyzer, "replaces": draft.route.value}
    return draft.model_copy(update={"route": Route.CU_ANALYZE, "locator": locator})


def _apply_enablement(draft: UnitDraft, policy: RoutingPolicy) -> UnitDraft:
    if draft.disposition is not None or draft.route in policy.enabled_routes:
        return draft
    return draft.model_copy(
        update={
            "disposition": UnitDisposition.UNSUPPORTED,
            "reason": f"route {draft.route.value} is disabled by policy {policy.version}",
        }
    )


def _pdf_units(report: PreflightReport, policy: RoutingPolicy) -> list[UnitDraft]:
    if not report.pages:
        return [
            UnitDraft(
                kind=UnitKind.WHOLE_FILE,
                route=Route.UNSUPPORTED,
                disposition=UnitDisposition.UNSUPPORTED,
                reason="PDF has no pages",
            )
        ]
    submit = (
        SubmitMode.SUBSET
        if report.size_bytes > min(policy.max_submit_bytes, policy.di_max_bytes)
        or (report.page_count or 0) > policy.di_max_pages
        else SubmitMode.PAGES
    )
    drafts: list[UnitDraft] = []
    for route, run in _runs((classify_page(page, policy), page) for page in report.pages):
        pages = [page.page for page in run]
        if route is Route.BLANK:
            drafts.append(
                UnitDraft(
                    kind=UnitKind.BLANK_PAGES,
                    route=route,
                    pages=tuple(pages),
                    disposition=UnitDisposition.NO_EVIDENCE,
                    reason="blank page: no text, image or vector objects",
                )
            )
            continue
        size, kind = {
            Route.NATIVE_PDF_TEXT: (policy.max_native_pages_per_unit, UnitKind.PDF_PAGES),
            Route.DI_LAYOUT: (policy.max_layout_pages_per_unit, UnitKind.PDF_PAGES),
            Route.DI_LAYOUT_DRAWING: (policy.max_drawing_pages_per_unit, UnitKind.DRAWING_SHEET),
        }[route]
        if route in DI_ROUTES:
            size = min(size, policy.di_max_pages)
        for start in range(0, len(pages), size):
            window = tuple(pages[start : start + size])
            selected = [page for page in run if page.page in window]
            locator: dict[str, Any] = {}
            if route in DI_ROUTES:
                locator["submit"] = submit.value
                locator["max_submit_bytes"] = min(policy.max_submit_bytes, policy.di_max_bytes)
            drafts.append(
                UnitDraft(
                    kind=kind,
                    route=route,
                    pages=window,
                    locator=locator,
                    signals={
                        "text_chars": sum(page.text_chars for page in selected),
                        "image_objects": sum(page.image_objects for page in selected),
                        "max_area_pt2": max(page.width_pt * page.height_pt for page in selected),
                    },
                )
            )
    return drafts


def _runs(
    classified: Iterable[tuple[Route, PageSignals]],
) -> list[tuple[Route, list[PageSignals]]]:
    runs: list[tuple[Route, list[PageSignals]]] = []
    for route, page in classified:
        if runs and runs[-1][0] is route:
            runs[-1][1].append(page)
        else:
            runs.append((route, [page]))
    return runs


def _whole_file_unit(report: PreflightReport, policy: RoutingPolicy) -> UnitDraft:
    family = report.family
    if family is Family.SPREADSHEET:
        mode: Literal["table", "independent_columns"] = (
            "independent_columns"
            if report.content_hash in policy.independent_column_workbooks else "table"
        )
        return UnitDraft(
            kind=UnitKind.SPREADSHEET,
            route=Route.NATIVE_SPREADSHEET,
            locator={
                "sheets": [sheet.name for sheet in report.sheets], "spreadsheet_mode": mode,
                **({"column_header_row": policy.independent_column_header_row}
                   if mode == "independent_columns" else {}),
            },
        )
    if family is Family.TEXT:
        return UnitDraft(
            kind=UnitKind.TEXT_FILE,
            route=Route.NATIVE_TEXT,
            locator={"format": _text_format(report.extension)},
        )
    if family is Family.BIM:
        return UnitDraft(kind=UnitKind.BIM_MODEL, route=Route.NATIVE_IFC)
    if report.file_kind is FileKind.DWG:
        return UnitDraft(kind=UnitKind.CAD_DRAWING, route=Route.NATIVE_DWG)
    if family in (Family.OFFICE, Family.IMAGE):
        route = Route.DI_LAYOUT_OFFICE if family is Family.OFFICE else Route.DI_LAYOUT
        kind = UnitKind.OFFICE_DOCUMENT if family is Family.OFFICE else UnitKind.IMAGE
        if report.size_bytes > min(policy.max_submit_bytes, policy.di_max_bytes):
            return UnitDraft(
                kind=kind,
                route=route,
                disposition=UnitDisposition.UNSUPPORTED,
                reason=f"{report.size_bytes} bytes exceeds the submit limit",
            )
        return UnitDraft(kind=kind, route=route, locator={
            "submit": SubmitMode.WHOLE.value,
            "max_submit_bytes": min(policy.max_submit_bytes, policy.di_max_bytes),
        })
    reasons = {
        Family.ARCHIVE: "archives are excluded by business policy",
        Family.EXECUTABLE: "executables are never analyzed",
        Family.EMPTY: "zero-byte source",
        Family.CAD: f"lineage only: {report.file_kind.value} is recorded without extraction",
    }
    return UnitDraft(
        kind=UnitKind.WHOLE_FILE,
        route=Route.UNSUPPORTED,
        disposition=UnitDisposition.UNSUPPORTED,
        reason=reasons.get(family, f"no extractor for {report.file_kind.value} content"),
    )


def _text_format(extension: str) -> str:
    return {".csv": "csv", ".tsv": "tsv", ".md": "markdown"}.get(extension, "text")
