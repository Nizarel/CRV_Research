"""Normalizers for natively parsed sources: PDF text layers, spreadsheets and text files."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence

from ...domain.errors import UnsupportedContentError
from ...domain.evidence import (
    BlockKind,
    CellKind,
    EvidenceBlock,
    EvidenceCell,
    EvidencePage,
    EvidenceTable,
    EvidenceUnit,
    Locator,
    LocatorKind,
    PageStatus,
    a1,
    block_id,
    detect_language,
    normalize_text,
)
from ...domain.ids import sha256_hex
from ...domain.models import UnitRecord
from ...ports.parsing import PdfPageText, SheetData

NATIVE_PDF = "native-pdf@1"
SPREADSHEET = "spreadsheet@1"
TEXT = "text@1"
REGION_GAP_ROWS = 2
"""This many empty rows split a sheet into separate table regions."""
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")


def _unit(
    unit: UnitRecord,
    *,
    plan_id: str,
    content_hash: str,
    method: str,
    normalizer: str,
    source_digest: str,
    pages: Sequence[EvidencePage],
    blocks: Sequence[EvidenceBlock],
    provider: dict[str, object] | None = None,
    warnings: Sequence[str] = (),
) -> EvidenceUnit:
    numbered = tuple(
        block.model_copy(update={"evidence_id": block_id(unit.unit_id, source_digest, index)})
        for index, block in enumerate(blocks)
    )
    text = "\n".join(block.text for block in numbered if block.text)
    return EvidenceUnit(
        unit_id=unit.unit_id,
        plan_id=plan_id,
        content_hash=content_hash,
        route=unit.route,
        extraction_method=method,
        normalizer=normalizer,
        provider=dict(provider or {}),
        pages=tuple(pages),
        blocks=numbered,
        language=detect_language(text),
        warnings=tuple(warnings),
    )


def _paragraphs(text: str) -> list[tuple[str, int, int]]:
    """Blank-line separated paragraphs with 1-based line ranges in ``text``."""

    paragraphs: list[tuple[str, int, int]] = []
    current: list[str] = []
    start = 0
    for number, line in enumerate(text.split("\n"), start=1):
        if line.strip():
            if not current:
                start = number
            current.append(line.strip())
        elif current:
            paragraphs.append((" ".join(current), start, number - 1))
            current = []
    if current:
        paragraphs.append((" ".join(current), start, start + len(current) - 1))
    return paragraphs


def normalize_native_pdf(
    pages: Sequence[PdfPageText], *, unit: UnitRecord, plan_id: str, content_hash: str
) -> EvidenceUnit:
    evidence_pages: list[EvidencePage] = []
    blocks: list[EvidenceBlock] = []
    digest_parts: list[str] = []
    for page in pages:
        text = normalize_text(page.text)
        digest_parts.append(sha256_hex(text))
        evidence_pages.append(
            EvidencePage(
                page=page.page,
                label=page.label,
                width=page.width_pt,
                height=page.height_pt,
                unit="pt",
                rotation=page.rotation,
                status=PageStatus.CONTENT if text else PageStatus.NO_TEXT,
                reason=None if text else "no native text on this page",
            )
        )
        for paragraph, first, last in _paragraphs(text):
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.PARAGRAPH,
                    text=paragraph,
                    pages=(page.page,),
                    locators=(
                        Locator(kind=LocatorKind.PAGE, page=page.page),
                        Locator(
                            kind=LocatorKind.LINES,
                            page=page.page,
                            line_start=first,
                            line_end=last,
                        ),
                    ),
                )
            )
    return _unit(
        unit,
        plan_id=plan_id,
        content_hash=content_hash,
        method="native_text",
        normalizer=NATIVE_PDF,
        source_digest=sha256_hex("|".join(digest_parts)),
        pages=evidence_pages,
        blocks=blocks,
        provider={"engine": "pdfium"},
    )


def _regions(cells: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Row ranges of table regions separated by ``REGION_GAP_ROWS`` or more empty rows."""

    rows = sorted({row for row, _ in cells})
    regions: list[tuple[int, int]] = []
    for row in rows:
        if regions and row - regions[-1][1] <= REGION_GAP_ROWS:
            regions[-1] = (regions[-1][0], row)
        else:
            regions.append((row, row))
    return regions


def normalize_spreadsheet(
    sheets: Sequence[SheetData], *, unit: UnitRecord, plan_id: str, content_hash: str,
    independent_column_header_row: int | None = None,
) -> EvidenceUnit:
    blocks: list[EvidenceBlock] = []
    warnings: list[str] = []
    digest_parts: list[str] = []
    prepared: list[SheetData] = []
    for data in sheets:
        if independent_column_header_row is None:
            prepared.append(data)
        else:
            for column in sorted({cell.column for cell in data.cells}):
                prepared.append(SheetData(
                    sheet=data.sheet,
                    cells=tuple(cell for cell in data.cells if cell.column == column),
                ))
            warnings.append(f"sheet {data.sheet.name!r}: columns are independent lists")
    for data in prepared:
        sheet = data.sheet
        hidden = sheet.visibility != "visible"
        if hidden:
            warnings.append(f"sheet {sheet.name!r} is {sheet.visibility}; included and flagged")
        by_position = {(cell.row, cell.column): cell for cell in data.cells}
        regions = (
            [(min(c.row for c in data.cells), max(c.row for c in data.cells))]
            if independent_column_header_row is not None and data.cells
            else _regions(list(by_position))
        )
        for first_row, last_row in regions:
            region = [cell for cell in data.cells if first_row <= cell.row <= last_row]
            first_column = min(cell.column for cell in region)
            last_column = max(cell.column for cell in region)
            spans: dict[tuple[int, int], tuple[int, int]] = {}
            for top, left, bottom, right in data.merged:
                if first_row <= top <= last_row and first_column <= left <= last_column:
                    spans[(top, left)] = (bottom - top + 1, right - left + 1)
            header_row = [cell for cell in region if cell.row == first_row]
            has_header = (
                last_row > first_row
                and bool(header_row)
                and all(cell.value_type == "string" for cell in header_row)
            )
            header_rows = (
                max(0, min(independent_column_header_row, last_row) - first_row + 1)
                if independent_column_header_row is not None else int(has_header)
            )
            evidence_cells = tuple(
                EvidenceCell(
                    row=cell.row - first_row,
                    column=cell.column - first_column,
                    row_span=spans.get((cell.row, cell.column), (1, 1))[0],
                    column_span=spans.get((cell.row, cell.column), (1, 1))[1],
                    kind=(
                        CellKind.COLUMN_HEADER
                        if cell.row < first_row + header_rows
                        else CellKind.CONTENT
                    ),
                    text=normalize_text(cell.text),
                    value_type=cell.value_type,
                    locator=Locator(
                        kind=LocatorKind.CELL,
                        sheet=sheet.name,
                        sheet_index=sheet.index,
                        cell=a1(cell.row, cell.column),
                        row=cell.row,
                        column=cell.column,
                        hidden=hidden,
                    ),
                )
                for cell in region
            )
            addressed = "|".join(
                f"{cell.locator.cell if cell.locator else ''}={cell.text}"
                for cell in evidence_cells
            )
            digest_parts.append(sha256_hex(f"{sheet.name}|{addressed}"))
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.TABLE,
                    locators=(
                        Locator(
                            kind=LocatorKind.CELL,
                            sheet=sheet.name,
                            sheet_index=sheet.index,
                            cell=a1(first_row, first_column),
                            cell_end=a1(last_row, last_column),
                            row=first_row,
                            column=first_column,
                            hidden=hidden,
                        ),
                    ),
                    table=EvidenceTable(
                        row_count=last_row - first_row + 1,
                        column_count=last_column - first_column + 1,
                        cells=evidence_cells,
                        header_rows=header_rows,
                        sheet=sheet.name,
                        hidden=hidden,
                    ),
                )
            )
    return _unit(
        unit,
        plan_id=plan_id,
        content_hash=content_hash,
        method="native_spreadsheet",
        normalizer=SPREADSHEET,
        source_digest=sha256_hex("|".join(digest_parts)),
        pages=(),
        blocks=blocks,
        provider={"engine": "calamine", "values": "cached", "formulas_evaluated": False},
        warnings=warnings,
    )


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace"), "cp1252"


def _looks_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d\s.,]+%?", text.strip()))


def normalize_text_file(
    data: bytes,
    *,
    text_format: str,
    unit: UnitRecord,
    plan_id: str,
    content_hash: str,
    max_cells: int,
) -> EvidenceUnit:
    text, encoding = decode_text(data)
    blocks: list[EvidenceBlock] = []
    if text_format in ("csv", "tsv"):
        blocks.append(_csv_table(text, tab=text_format == "tsv", max_cells=max_cells))
    else:
        for paragraph, first, last in _paragraphs(normalize_text(text)):
            heading = _HEADING.match(paragraph) if text_format == "markdown" else None
            locator = Locator(kind=LocatorKind.LINES, line_start=first, line_end=last)
            if heading:
                blocks.append(
                    EvidenceBlock(
                        evidence_id="pending",
                        kind=BlockKind.HEADING,
                        text=heading.group(2),
                        level=len(heading.group(1)),
                        locators=(locator,),
                    )
                )
            else:
                blocks.append(
                    EvidenceBlock(
                        evidence_id="pending",
                        kind=BlockKind.PARAGRAPH,
                        text=paragraph,
                        locators=(locator,),
                    )
                )
    return _unit(
        unit,
        plan_id=plan_id,
        content_hash=content_hash,
        method="native_text",
        normalizer=TEXT,
        source_digest=content_hash,
        pages=(),
        blocks=blocks,
        provider={"encoding": encoding, "format": text_format},
    )


def _csv_table(text: str, *, tab: bool, max_cells: int) -> EvidenceBlock:
    sample = text[:65536]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            sample, delimiters="\t" if tab else ",;\t|"
        )
    except csv.Error:
        dialect = csv.excel_tab if tab else csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    cells: list[EvidenceCell] = []
    first_line: list[int] = []
    columns = 0
    previous_line = 0
    for record, row in enumerate(reader, start=1):
        first_line.append(previous_line + 1)
        previous_line = reader.line_num
        columns = max(columns, len(row))
        for column, value in enumerate(row, start=1):
            value = normalize_text(value)
            if not value:
                continue
            cells.append(
                EvidenceCell(
                    row=record - 1,
                    column=column - 1,
                    text=value,
                    value_type="string",
                    locator=Locator(
                        kind=LocatorKind.CELL,
                        cell=a1(record, column),
                        row=record,
                        column=column,
                        line_start=first_line[-1],
                        line_end=previous_line,
                    ),
                )
            )
        if len(cells) > max_cells:
            raise UnsupportedContentError(f"Delimited file exceeds {max_cells} cells")
    records = len(first_line)
    header = [cell for cell in cells if cell.row == 0]
    has_header = records > 1 and bool(header) and not any(_looks_numeric(c.text) for c in header)
    if has_header:
        cells = [
            cell.model_copy(update={"kind": CellKind.COLUMN_HEADER}) if cell.row == 0 else cell
            for cell in cells
        ]
    return EvidenceBlock(
        evidence_id="pending",
        kind=BlockKind.TABLE,
        locators=(Locator(kind=LocatorKind.LINES, line_start=1, line_end=max(previous_line, 1)),),
        table=EvidenceTable(
            row_count=records,
            column_count=columns,
            cells=tuple(cells),
            header_rows=1 if has_header else 0,
        ),
    )
