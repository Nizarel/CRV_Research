"""Provider-neutral canonical evidence with immutable original locators.

Every block keeps where it came from in the source: original 1-based page numbers, regions
with their coordinate unit and origin, original spreadsheet A1 addresses, or text spans that
name the exact text representation and index type they point into. Spans are never assumed to
index cleaned text.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import Sha256Hex, sha256_hex
from .models import ArtifactRef

UNIT_SCHEMA = "evidence-unit.v1"
DOCUMENT_SCHEMA = "evidence-document.v1"
TEXT_NORMALIZATION = "nfc-lf-v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceKind(StrEnum):
    SOURCE_TEXT = "source_text"
    EXTRACTED_FIELD = "extracted_field"
    CLASSIFIED_LABEL = "classified_label"
    GENERATED_DESCRIPTION = "generated_description"
    DERIVED_CALCULATION = "derived_calculation"


class LocatorKind(StrEnum):
    PAGE = "page"
    REGION = "region"
    CELL = "cell"
    SPAN = "span"
    LINES = "lines"
    DOCUMENT = "document"


class Locator(_Frozen):
    """One place in the original source. Pages, rows and columns are 1-based originals."""

    kind: LocatorKind
    page: int | None = Field(default=None, ge=1)
    polygon: tuple[float, ...] = ()
    unit: str | None = None
    """Coordinate unit of ``polygon`` and page dimensions: ``inch``, ``pixel`` or ``pt``."""
    page_width: float | None = None
    page_height: float | None = None
    sheet: str | None = None
    sheet_index: int | None = Field(default=None, ge=0)
    cell: str | None = None
    cell_end: str | None = None
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    hidden: bool = False
    artifact_sha256: Sha256Hex | None = None
    representation: str | None = None
    index_type: str | None = None
    offset: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=0)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        required: dict[LocatorKind, tuple[str, ...]] = {
            LocatorKind.PAGE: ("page",),
            LocatorKind.REGION: ("page", "unit"),
            LocatorKind.CELL: ("cell", "row", "column"),
            LocatorKind.SPAN: ("artifact_sha256", "representation", "index_type", "offset"),
            LocatorKind.LINES: ("line_start", "line_end"),
            LocatorKind.DOCUMENT: (),
        }
        missing = [name for name in required[self.kind] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind.value} locator is missing {', '.join(missing)}")
        if self.kind is LocatorKind.REGION and len(self.polygon) % 2:
            raise ValueError("A polygon needs an even number of coordinates")
        return self

    def compact(self) -> str:
        """Short, stable string for search projections and citations."""

        if self.kind is LocatorKind.CELL:
            single = self.cell_end in (None, self.cell)
            where = self.cell if single else f"{self.cell}:{self.cell_end}"
            sheet = f"{self.sheet}!" if self.sheet else ""
            return f"cell:{sheet}{where}"
        if self.kind is LocatorKind.REGION:
            box = ",".join(f"{value:.4g}" for value in bounding_box(self.polygon))
            return f"region:p{self.page}:{self.unit}:{box}"
        if self.kind is LocatorKind.SPAN:
            return f"span:{self.representation}:{self.offset}+{self.length or 0}"
        if self.kind is LocatorKind.LINES:
            return f"lines:{self.line_start}-{self.line_end}"
        if self.kind is LocatorKind.PAGE:
            return f"page:{self.page}"
        # CAD drawings cite the layout (sheet) a value was read from.
        return f"sheet:{self.sheet}" if self.sheet else "document"


def bounding_box(polygon: tuple[float, ...]) -> tuple[float, float, float, float]:
    xs, ys = polygon[0::2], polygon[1::2]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


class BlockKind(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"
    PAGE_NUMBER = "page_number"


FURNITURE = frozenset({BlockKind.PAGE_HEADER, BlockKind.PAGE_FOOTER, BlockKind.PAGE_NUMBER})
"""Repeated page furniture: kept as evidence, excluded from chunk text."""


class CellKind(StrEnum):
    CONTENT = "content"
    COLUMN_HEADER = "columnHeader"
    ROW_HEADER = "rowHeader"
    STUB_HEAD = "stubHead"
    DESCRIPTION = "description"


class EvidenceCell(_Frozen):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    kind: CellKind = CellKind.CONTENT
    text: str = ""
    value_type: str | None = None
    """Native spreadsheets: ``string``, ``number``, ``bool``, ``date``, ``time`` or ``empty``."""
    locator: Locator | None = None


class EvidenceTable(_Frozen):
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    cells: tuple[EvidenceCell, ...]
    header_rows: int = Field(default=0, ge=0)
    caption: str | None = None
    footnotes: tuple[str, ...] = ()
    sheet: str | None = None
    hidden: bool = False

    def grid(self) -> list[list[str]]:
        """Dense text grid; spanned cells repeat their text so every row stands alone."""

        grid = [["" for _ in range(self.column_count)] for _ in range(self.row_count)]
        for cell in self.cells:
            for row in range(cell.row, min(cell.row + cell.row_span, self.row_count)):
                for column in range(
                    cell.column, min(cell.column + cell.column_span, self.column_count)
                ):
                    grid[row][column] = cell.text
        return grid

    def cell_at(self, row: int, column: int) -> EvidenceCell | None:
        for cell in self.cells:
            if (
                cell.row <= row < cell.row + cell.row_span
                and cell.column <= column < cell.column + cell.column_span
            ):
                return cell
        return None


class EvidenceFigure(_Frozen):
    figure_id: str
    caption: str | None = None


class EvidenceBlock(_Frozen):
    evidence_id: str
    kind: BlockKind
    text: str = ""
    level: int | None = Field(default=None, ge=1, le=9)
    role: str | None = None
    pages: tuple[int, ...] = ()
    locators: tuple[Locator, ...] = ()
    table: EvidenceTable | None = None
    figure: EvidenceFigure | None = None
    evidence_kind: EvidenceKind = EvidenceKind.SOURCE_TEXT
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Self:
        if (self.kind is BlockKind.TABLE) != (self.table is not None):
            raise ValueError("Only table blocks carry (and require) a table")
        if (self.kind is BlockKind.FIGURE) != (self.figure is not None):
            raise ValueError("Only figure blocks carry (and require) a figure")
        if self.kind in (BlockKind.HEADING, BlockKind.TITLE) and self.level is None:
            raise ValueError("Headings need a level")
        return self


class PageStatus(StrEnum):
    CONTENT = "content"
    BLANK = "blank"
    NO_TEXT = "no_text"
    MISSING = "missing"


class EvidencePage(_Frozen):
    page: int = Field(ge=1)
    """Original 1-based page index; never renumbered."""
    label: str | None = None
    width: float | None = None
    height: float | None = None
    unit: str | None = None
    rotation: float | None = None
    status: PageStatus = PageStatus.CONTENT
    reason: str | None = None


class FieldObservation(_Frozen):
    """One leaf value of an analyzer field, with where it was read and how it was produced."""

    path: str
    """``Tags[2]`` or ``StreamRows[0].RawValue``; top-level name first."""
    field: str
    method: str
    evidence_kind: EvidenceKind
    value_type: str
    value: Any = None
    """As returned by the service, which normalizes typed values."""
    raw_text: str | None = None
    """Printed text recovered from the field spans; ``None`` when the field has no spans."""
    confidence: float | None = Field(default=None, ge=0, le=1)
    pages: tuple[int, ...] = ()
    locators: tuple[Locator, ...] = ()
    analyzer_id: str


class EvidenceUnit(_Frozen):
    """Normalized evidence of one extraction unit (stored as one immutable artifact)."""

    schema_name: str = UNIT_SCHEMA
    unit_id: str
    plan_id: str
    content_hash: Sha256Hex
    route: str
    extraction_method: str
    normalizer: str
    text_normalization: str = TEXT_NORMALIZATION
    provider: dict[str, Any] = Field(default_factory=dict)
    raw_artifact: ArtifactRef | None = None
    pages: tuple[EvidencePage, ...] = ()
    blocks: tuple[EvidenceBlock, ...] = ()
    language: str | None = None
    warnings: tuple[str, ...] = ()
    fields: tuple[FieldObservation, ...] = ()

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [block.evidence_id for block in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("Evidence block IDs must be unique within a unit")
        pages = [page.page for page in self.pages]
        if pages != sorted(set(pages)):
            raise ValueError("Evidence pages must be sorted and unique")
        return self

    @property
    def has_content(self) -> bool:
        return bool(self.fields) or any(block.text.strip() or block.table for block in self.blocks)


class UnitSummary(_Frozen):
    unit_id: str
    ordinal: int
    route: str
    kind: str
    pages: tuple[int, ...] = ()
    disposition: str
    reason: str | None = None
    evidence: ArtifactRef | None = None


class DocumentPage(_Frozen):
    page: int = Field(ge=1)
    status: PageStatus
    unit_id: str | None = None
    reason: str | None = None


class EvidenceDocument(_Frozen):
    """Manifest assembling unit evidence in original order; units stay separate artifacts."""

    schema_name: str = DOCUMENT_SCHEMA
    evidence_id: str
    plan_id: str
    content_hash: Sha256Hex
    policy_version: str
    family: str
    page_count: int | None = None
    units: tuple[UnitSummary, ...]
    pages: tuple[DocumentPage, ...] = ()
    completeness: str
    document_codes: tuple[str, ...] = ()
    """Codes printed in page headers or repeated like them: the document's own identity."""

    @property
    def complete(self) -> bool:
        return self.completeness == "complete"


def block_id(unit_id: str, source_digest: str, index: int) -> str:
    """Stable evidence ID: same unit, same raw source and same position give the same ID."""

    return "ev:" + sha256_hex(f"{unit_id}|{source_digest}|{index}")[:32]


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_text(text: str) -> str:
    """``TEXT_NORMALIZATION``: NFC, LF line ends, no control characters, trimmed lines."""

    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


_FRENCH = frozenset(
    {
        "le", "la", "les", "des", "une", "est", "et", "du", "au", "aux", "pour", "par",
        "sur", "dans", "avec", "ce", "cette", "sont", "pas", "qui", "que", "ou", "en", "de",
    }
)
_ENGLISH = frozenset(
    {
        "the", "and", "of", "to", "in", "is", "for", "on", "with", "this", "that", "are",
        "be", "by", "as", "or", "from", "at", "an", "it", "not",
    }
)


def detect_language(text: str) -> str | None:
    """Cheap stop-word vote between French and English; ``None`` when undecided."""

    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ]+", text.lower())[:2000]
    french = sum(word in _FRENCH for word in words)
    english = sum(word in _ENGLISH for word in words)
    if max(french, english) < 3:
        return None
    if french >= english * 1.5:
        return "fr"
    if english >= french * 1.5:
        return "en"
    return None


def a1(row: int, column: int) -> str:
    """1-based row/column to an A1 address (``(2, 2)`` -> ``B2``)."""

    if row < 1 or column < 1:
        raise ValueError("A1 addresses are 1-based")
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return f"{letters}{row}"
