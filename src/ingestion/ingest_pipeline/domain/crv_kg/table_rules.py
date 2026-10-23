"""KG-2 table rules: a numeric table cell becomes an Observation only when its headers say what
it measures (design §5, "Observation extraction"). Ambiguous cells are skipped, never guessed.

Mass-balance tables put streams in rows (feed, concentrates, tailings) and quantities in columns
(weight, grade, distribution or recovery per element); the rules read the column headers for
the property and element and the row label (or headers) for the stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from ..observations import ScientificObservation
from .study import Property, Stream
from .vocab import fold

ELEMENTS = ("cu", "ag", "au", "fe", "zn", "pb", "co", "as", "sb", "ni", "s", "cuox", "cus")
_PROPERTIES: tuple[tuple[str, Property], ...] = (
    (r"\bwork index\b|\bbwi\b|\bwi\b", "work-index"),
    (r"\bd80\b|\bd 80\b", "d80"),
    (r"\btaux d oxydation\b|\boxydation\b|\boxidation\b", "oxidation-rate"),
    (r"\bcu fe\b|\bfe cu\b", "cu-fe-ratio"),
    (r"\bconsommation d acide\b|\bacid consumption\b", "acid-consumption"),
    (r"\bdistribution\b|\brepartition\b", "distribution"),
    (r"\brendement ponderal\b|\bpoids\b|\bmasse\b|\bweight\b|\byield\b", "weight-yield"),
    (r"\brecuperation\b|\brecovery\b|\brendement\b|\brec\b", "recovery"),
    (r"\bph\b", "ph"),
    (r"\bdosage\b|\bdose\b|\bconsommation\b|\bg t reactif\b", "reagent-consumption"),
    (r"\btonnage\b|\btonnes\b|\btraite\b", "tonnage"),
)
_STREAMS: tuple[tuple[str, Stream], ...] = (
    (r"\bebauch\w*\b|\brougher\b", "rougher-concentrate"),
    (r"\bfinisseu\w*\b|\brelavage\b|\bcleaner\b", "cleaner-concentrate"),
    (r"\bepuis\w*\b|\bscavenger\b", "scavenger"),
    (r"\bmixte\w*\b|\bmiddling\w*\b", "middlings"),
    (r"\bconcentre\w*\b|\bconc\b|\bconcentrate\b", "concentrate"),
    (r"\brejet\w*\b|\bsterile\w*\b|\btailings?\b|\bresidu\w*\b", "tailings"),
    (r"\balimentation\b|\btout venant\b|\btv\b|\bfeed\b|\bhead\b", "feed"),
)
_GRADE = re.compile(r"\bteneurs?\b|\bgrades?\b|\btitres?\b|\bassays?\b")
_UNITS = {"%": "%", "g/t": "g/t", "ppm": "ppm", "ppb": "ppb", "mg/kg": "mg/kg", "mg/l": "mg/l",
          "g/l": "g/l"}


@dataclass(frozen=True)
class TableObservation:
    property: Property
    stream: Stream | None
    element: str | None
    value_text: str
    value: Decimal
    unit: str | None
    qualifier: str | None
    quote: str
    """Header, row label and cell as printed: what the observation is located by."""


def _first(patterns: tuple[tuple[str, str], ...], text: str) -> str | None:
    return next((name for pattern, name in patterns if re.search(pattern, text)), None)


def _element(text: str) -> str | None:
    found = [token for token in text.split() if token in ELEMENTS]
    return found[0].capitalize() if len(set(found)) == 1 else None


def table_observation(cell: ScientificObservation) -> TableObservation | None:
    """The cell as a typed observation, or None when its headers do not say what it is."""

    headers = fold(" ".join(cell.column_headers))
    row = fold(cell.row_label or "")
    if not headers:
        return None
    measured = _first(_PROPERTIES, headers)
    stream = _first(_STREAMS, row) or _first(_STREAMS, headers)
    if measured is None and _GRADE.search(headers):
        if stream in {"concentrate", "rougher-concentrate", "cleaner-concentrate", "scavenger"}:
            measured = "concentrate-grade"
        elif stream == "feed":
            measured = "head-grade"
    if measured is None:
        return None
    element = _element(headers) or _element(row)
    if measured in {"recovery", "distribution", "concentrate-grade", "head-grade"} and not element:
        return None
    unit = _UNITS.get((cell.unit_text or "").lower()) or next(
        (unit for key, unit in _UNITS.items() if key in " ".join(cell.column_headers).lower()),
        None,
    )
    label = " / ".join(dict.fromkeys(
        part for part in (*cell.column_headers, cell.row_label) if part
    ))
    return TableObservation(
        property=cast(Property, measured),
        stream=cast(Stream | None, stream),
        element=element,
        value_text=cell.raw_value.strip(),
        value=cell.value,
        unit=unit,
        qualifier=cell.qualifier,
        quote=f"{label}: {cell.raw_value.strip()}",
    )
