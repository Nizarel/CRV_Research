"""Native spreadsheet access through python-calamine (MIT).

Calamine skips leading empty rows and columns; every value is re-addressed with the sheet's
``start`` offset so original A1 addresses survive. Only cached formula results are read: no
formula is evaluated and no macro runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from ...domain.errors import ResourceExhaustedError, UnsupportedContentError
from ...domain.routing import SheetSignals
from ...ports.parsing import SheetCell, SheetData


def render_value(value: Any) -> tuple[str, str]:
    """Text and type of one cached cell value; a blank cell is never rendered as zero."""

    if value is None or value == "":
        return "", "empty"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), "bool"
    if isinstance(value, int):
        return str(value), "number"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value)), "number"
        return repr(value), "number"
    if isinstance(value, dt.datetime):
        return value.isoformat(), "datetime"
    if isinstance(value, dt.date):
        return value.isoformat(), "date"
    if isinstance(value, dt.time):
        return value.isoformat(), "time"
    if isinstance(value, dt.timedelta):
        return str(value), "duration"
    return str(value), "string"


def _visibility(metadata: Any) -> str:
    return str(metadata.visible).rsplit(".", 1)[-1].lower().replace("veryhidden", "very_hidden")


def _is_worksheet(metadata: Any) -> bool:
    return str(metadata.typ).rsplit(".", 1)[-1].lower() == "worksheet"


def _open(path: Path) -> Any:
    import python_calamine

    try:
        return python_calamine.CalamineWorkbook.from_path(str(path))
    except python_calamine.PasswordError as error:
        raise UnsupportedContentError("Workbook is password protected") from error
    except (python_calamine.CalamineError, OSError, ValueError) as error:
        raise UnsupportedContentError(f"Workbook cannot be opened: {error}") from error


def _profile(path: Path) -> list[SheetSignals]:
    workbook = _open(path)
    try:
        sheets: list[SheetSignals] = []
        for index, metadata in enumerate(workbook.sheets_metadata):
            rows = columns = 0
            if _is_worksheet(metadata):
                sheet = workbook.get_sheet_by_name(metadata.name)
                rows, columns = int(sheet.height), int(sheet.width)
            sheets.append(
                SheetSignals(
                    name=metadata.name,
                    index=index,
                    visibility=_visibility(metadata),
                    rows=rows,
                    columns=columns,
                )
            )
        return sheets
    finally:
        workbook.close()


def _read(path: Path, include_hidden: bool, max_cells: int) -> list[SheetData]:
    workbook = _open(path)
    try:
        budget = max_cells
        results: list[SheetData] = []
        for index, metadata in enumerate(workbook.sheets_metadata):
            visibility = _visibility(metadata)
            if not _is_worksheet(metadata) or (visibility != "visible" and not include_hidden):
                continue
            sheet = workbook.get_sheet_by_name(metadata.name)
            height, width = int(sheet.height), int(sheet.width)
            budget -= height * width
            if budget < 0:
                raise ResourceExhaustedError(f"Workbook exceeds {max_cells} cells")
            signals = SheetSignals(
                name=metadata.name, index=index, visibility=visibility, rows=height, columns=width
            )
            start = sheet.start
            if start is None:
                results.append(SheetData(sheet=signals, cells=()))
                continue
            first_row, first_column = int(start[0]) + 1, int(start[1]) + 1
            cells: list[SheetCell] = []
            for row_offset, row in enumerate(sheet.to_python()):
                for column_offset, value in enumerate(row):
                    text, value_type = render_value(value)
                    if value_type == "empty":
                        continue
                    cells.append(
                        SheetCell(
                            row=first_row + row_offset,
                            column=first_column + column_offset,
                            text=text,
                            value_type=value_type,
                        )
                    )
            merged = tuple(
                (int(top[0]) + 1, int(top[1]) + 1, int(bottom[0]) + 1, int(bottom[1]) + 1)
                for top, bottom in (sheet.merged_cell_ranges or ())
            )
            results.append(SheetData(sheet=signals, cells=tuple(cells), merged=merged))
        return results
    finally:
        workbook.close()


class CalamineSpreadsheetReader:
    """``SpreadsheetReader`` over python-calamine (XLSX, XLSM, XLSB, XLS, ODS)."""

    async def profile(self, path: Path) -> list[SheetSignals]:
        return await asyncio.to_thread(_profile, path)

    async def read(self, path: Path, *, include_hidden: bool, max_cells: int) -> list[SheetData]:
        return await asyncio.to_thread(_read, path, include_hidden, max_cells)
