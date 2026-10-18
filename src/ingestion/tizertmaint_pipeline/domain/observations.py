"""Conservative numeric cell observations; no inferred units, chemistry or cross-row joins."""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import Field

from .evidence import EvidenceUnit, Locator
from .models import Frozen

_NUMBER = re.compile(
    r"^\s*(?P<qualifier><=|>=|<|>|~)?\s*"
    r"(?P<value>[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*(?P<unit>%|mg/kg|mg/l|g/t|g/l|ppm|ppb)?\s*$",
    re.IGNORECASE,
)


class ScientificObservation(Frozen):
    evidence_id: str
    unit_id: str
    table_row: int = Field(ge=0)
    table_column: int = Field(ge=0)
    raw_value: str
    value: Decimal
    qualifier: str | None = None
    unit_text: str | None = None
    column_headers: tuple[str, ...] = ()
    row_label: str | None = None
    caption: str | None = None
    footnotes: tuple[str, ...] = ()
    locators: tuple[Locator, ...]
    status: str = "unreviewed"


def numeric_observations(unit: EvidenceUnit) -> list[ScientificObservation]:
    result: list[ScientificObservation] = []
    for block in unit.blocks:
        table = block.table
        if table is None:
            continue
        grid = table.grid()
        for cell in table.cells:
            if cell.row < table.header_rows:
                continue
            match = _NUMBER.fullmatch(cell.text)
            if match is None:
                continue
            # Without a declared locale, 1,234 and 1.234 may mean decimals or thousands.
            if re.fullmatch(r"[+-]?[1-9]\d{0,2}[.,]\d{3}", match["value"]):
                continue
            locators = (cell.locator,) if cell.locator else block.locators
            if not locators:
                continue
            result.append(ScientificObservation(
                evidence_id=block.evidence_id, unit_id=unit.unit_id,
                table_row=cell.row, table_column=cell.column,
                raw_value=cell.text, value=Decimal(match["value"].replace(",", ".")),
                qualifier=match["qualifier"], unit_text=match["unit"],
                column_headers=tuple(dict.fromkeys(
                    grid[row][cell.column] for row in range(table.header_rows)
                    if grid[row][cell.column]
                )),
                row_label=grid[cell.row][0] if cell.column else None,
                caption=table.caption, footnotes=table.footnotes, locators=locators,
            ))
    return result
