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

ELEMENTS = ("cu", "ag", "au", "fe", "zn", "pb", "co", "as", "sb", "ni", "s", "cuox", "cus",
            "bi", "mo", "mn", "hg", "cd", "sn")
_SYMBOLS = {"cuox": "CuOx", "cus": "CuS"}
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
    (r"\bconcentre\w*\b|\bconc\b|\bcon\b|\bconcentrate\b", "concentrate"),
    (r"\brejet\w*\b|\bsterile\w*\b|\btailings?\b|\bresidu\w*\b", "tailings"),
    (r"\balimentation\b|\balim\b|\btout venant\b|\btv\b|\bfeed\b|\bhead\b", "feed"),
)
_GRADE = re.compile(r"\bteneurs?\b|\bgrades?\b|\btitres?\b|\bassays?\b|\banalyses?\b")
# Operating hours and moisture sit beside tonnages ("Tonnage / Heures de marche", "H%").
_NOT_MEASURED = re.compile(r"\bheures?\b|\bhours?\b|\bhumidite\b|\bmoisture\b|^h$")
# A stream is named in a few words; a longer row label is a test condition
# ("Prolongation du temps de séjour (ajout d'un épuisement)"), not a stream.
_STREAM_WORDS = 6
_UNITS = {"%": "%", "percent": "%", "g/t": "g/t", "ppm": "ppm", "ppb": "ppb", "mg/kg": "mg/kg",
          "mg/l": "mg/l", "g/l": "g/l"}
_UNIT_PATTERNS = tuple(
    (re.compile(r"(?<![a-z])" + re.escape(key)), unit) for key, unit in _UNITS.items()
)
_SHARES = frozenset({"recovery", "distribution", "weight-yield", "oxidation-rate"})
_BY_ELEMENT = frozenset({"recovery", "distribution", "concentrate-grade", "head-grade"})


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
    folded = re.sub(r"\bcu ox\w*", "cuox", text)
    found = [token for token in folded.split() if token in ELEMENTS]
    if len(set(found)) != 1:
        return None
    return _SYMBOLS.get(found[0], found[0].capitalize())


def _row_element(label: str) -> str | None:
    """An element named in a row label, written as a symbol ("Sb"): sample codes ("RC 211 SB")
    are upper case."""

    symbols = " ".join(
        token for token in re.findall(r"[A-Za-z]+", label) if token == token.capitalize()
    )
    return _element(symbols.lower())


def _units(header: str) -> set[str]:
    return {unit for pattern, unit in _UNIT_PATTERNS if pattern.search(header.lower())}


def table_observation(cell: ScientificObservation) -> TableObservation | None:
    """The cell as a typed observation, or None when its headers do not say what it is."""

    # A header stating a test condition ("D80 = 45 µm") says how the column was measured, not what.
    headings = [header for header in cell.column_headers if "=" not in header]
    headers = fold(" ".join(headings))
    row = fold(cell.row_label or "")
    if not headers or _NOT_MEASURED.search(fold(headings[-1])):
        return None
    measured = _first(_PROPERTIES, headers)
    row_stream = _first(_STREAMS, row) if len(row.split()) <= _STREAM_WORDS else None
    stream = row_stream or _first(_STREAMS, headers)
    if measured is None and _GRADE.search(headers):
        if stream in {"concentrate", "rougher-concentrate", "cleaner-concentrate", "scavenger"}:
            measured = "concentrate-grade"
        elif stream == "feed":
            measured = "head-grade"
    if measured is None:
        return None
    # The innermost header naming a unit says the column's unit ("(% ou ppm) / Ag (ppm)" is ppm);
    # one offering two ("Teneur (%) ou ppm") leaves the cell ambiguous.
    named = next((units for units in map(_units, reversed(headings)) if units), set())
    unit = _UNITS.get((cell.unit_text or "").lower())
    if unit is None and len(named) > 1:
        return None
    unit = unit or next(iter(named), None)
    # Shares are percentages: a mass ("Poids (Kg)") or a grade ("Rec (ppm)") is not one.
    if measured in _SHARES and unit not in {"%", None}:
        return None
    if measured == "weight-yield" and unit != "%":
        return None
    element = None
    if measured in _BY_ELEMENT:
        element = _element(headers) or _row_element(cell.row_label or "")
        if not element:
            return None
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
