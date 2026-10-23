"""KG-2 pilot records extracted per document: studies, samples and observations.

The extractor returns a ``DocumentExtraction`` (strict JSON schema, ``openai_schema``). Every
study, sample, reagent use and observation carries a ``Locator``: a page and a verbatim quote
of at most ``QUOTE_CHARS``. ``ground`` keeps only items whose quote is on the cited page (after
``normalize_text``) and observations whose printed value occurs in their quote; the numeric
value is parsed here, never taken from the model. Grounded items become ``candidate`` records
(Docs/KNOWLEDGE_GRAPH_DESIGN.md §3, §5, §8): an unreviewed extraction is never ``observed``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .table_rules import TableObservation

QUOTE_CHARS = 400
VALUE_CHARS = 120
LEAD_PAGES = 6
"""Cover, introduction and contents: where scale, facility, samples and period are stated."""
TAIL_PAGES = 3
"""Conclusions: where the reported results are summarised."""

Scale = Literal[
    "desk",
    "lab-orientation",
    "lab-optimisation",
    "variability",
    "mini-pilot",
    "pilot",
    "industrial-trial",
    "plant-monitoring",
]
Process = Literal[
    "crushing-grinding",
    "flotation",
    "dms",
    "gravity",
    "whims",
    "leaching",
    "cyanidation",
    "sx",
    "ew",
    "cementation",
    "detalcing",
    "roasting",
    "filtration",
    "decantation",
]
MaterialKind = Literal[
    "oxide-ore",
    "sulphide-ore",
    "mixed-ore",
    "composite",
    "tailings",
    "slag",
    "halde",
    "cement",
    "concentrate",
    "black-mass",
    "hydroxide",
    "effluent",
    "secondary-material",
]
ReagentRole = Literal[
    "collector",
    "depressant",
    "sulphidiser",
    "frother",
    "activator",
    "ph-modifier",
    "dispersant",
    "lixiviant",
    "oxidant",
    "extractant",
    "flocculant",
]
# Pilot property list: design §3.3 plus the quantities the B/C/D golden questions compare.
Property = Literal[
    "head-grade",
    "oxidation-rate",
    "recovery",
    "concentrate-grade",
    "distribution",
    "weight-yield",
    "work-index",
    "d80",
    "acid-consumption",
    "reagent-consumption",
    "ph",
    "cu-fe-ratio",
    "tonnage",
    "throughput",
    "resource-tonnage",
    "cost",
    "energy",
    "mineral-abundance",
]
Stream = Literal[
    "feed",
    "concentrate",
    "rougher-concentrate",
    "cleaner-concentrate",
    "scavenger",
    "middlings",
    "tailings",
    "final",
    "leach-solution",
    "residue",
]
Basis = Literal[
    "dry",
    "wet",
    "per-t-tv",
    "kwh-per-t",
    "kwh-per-sht",
    "mine-to-cathode",
    "reagents-only",
    "opex",
    "capex",
    "cut-off",
]
Qualifier = Literal["<", "<=", ">", ">=", "~"]

_SPACES = re.compile(r"\s+")
_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2011": "-",
        "\u2212": "-",
        "\u00ad": None,
    }
)
_GROUPED = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")
_SINGLE_NUMBER = re.compile(r"\D*?([+-]?\d+(?:[.,]\d+)?)\D*")


def normalize_text(text: str) -> str:
    """NFKC, typographic quotes and dashes to ASCII, soft hyphens dropped, spaces collapsed."""

    folded = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION)
    return _SPACES.sub(" ", folded).strip()


def parse_value(value_text: str) -> Decimal | None:
    """The single number of an as-printed value (decimal comma, grouped thousands); None for
    ranges, lists and words."""

    text = _GROUPED.sub("", normalize_text(value_text))
    match = _SINGLE_NUMBER.fullmatch(text)
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Locator(_Strict):
    page: int
    quote: str
    """Shortest verbatim span of that page stating the item (under 400 characters)."""


class SampleRef(_Strict):
    name: str
    origin: str | None
    material_kind: MaterialKind | None
    mass_kg: float | None
    locator: Locator


class ReagentUse(_Strict):
    name: str
    role: ReagentRole | None
    dose_text: str | None
    locator: Locator


class StudyExtraction(_Strict):
    label: str
    scale: Scale
    period: str | None
    facility: str | None
    commissioned_by: str | None
    partners: list[str]
    commodities: list[str]
    processes: list[Process]
    samples: list[SampleRef]
    reagents: list[ReagentUse]
    follows_or_validates: list[str]
    locator: Locator


class ObservationExtraction(_Strict):
    study: int | None
    """Index of the reporting study in ``studies``, or None."""
    sample: str | None
    site: str | None
    property: Property
    element: str | None
    stream: Stream | None
    basis: Basis | None
    value_text: str
    unit: str | None
    qualifier: Qualifier | None
    locator: Locator


class DocumentExtraction(_Strict):
    studies: list[StudyExtraction]
    observations: list[ObservationExtraction]


def vocabulary(kind: Any) -> tuple[str, ...]:
    return tuple(get_args(kind))


def study_pages(page_count: int) -> tuple[int, ...]:
    """Pages read per document (design §5): the first six and the last three."""

    last = range(max(1, page_count - TAIL_PAGES + 1), page_count + 1)
    return tuple(sorted({*range(1, min(LEAD_PAGES, page_count) + 1), *last}))


@dataclass(frozen=True)
class StudyDocument:
    content_hash: str
    source_path: str
    page_count: int
    pages: Mapping[int, str]
    """Text of the selected pages, keyed by 1-based page number."""
    tables: tuple[tuple[int, TableObservation], ...] = ()
    """Typed numeric table cells of every page, with their page number (table rules)."""


def openai_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strict structured-output schema: every property required, no extras, no defaults."""

    def strict(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: strict(v) for k, v in node.items() if k not in {"default", "title"}}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [strict(item) for item in node]
        return node

    schema: dict[str, Any] = strict(model.model_json_schema())
    return schema


@dataclass(frozen=True)
class GroundedObservation:
    observation: ObservationExtraction
    value_num: Decimal | None


@dataclass
class Grounding:
    studies: list[StudyExtraction] = field(default_factory=list)
    observations: list[GroundedObservation] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def located(locator: Locator, pages: Mapping[int, str]) -> bool:
    quote = normalize_text(locator.quote)
    return (
        0 < len(locator.quote) <= QUOTE_CHARS
        and bool(quote)
        and quote in normalize_text(pages.get(locator.page, ""))
    )


def ground(extraction: DocumentExtraction, pages: Mapping[int, str]) -> Grounding:
    """Keep only what the cited pages state; ``rejected`` explains every dropped item."""

    result = Grounding()
    kept: dict[int, int] = {}
    for n, study in enumerate(extraction.studies):
        if not located(study.locator, pages):
            result.rejected.append(f"study {n}: quote not on page {study.locator.page}")
            continue
        samples = [s for s in study.samples if located(s.locator, pages)]
        reagents = [r for r in study.reagents if located(r.locator, pages)]
        dropped = len(study.samples) - len(samples) + len(study.reagents) - len(reagents)
        if dropped:
            result.rejected.append(f"study {n}: {dropped} sample/reagent quotes not on page")
        kept[n] = len(result.studies)
        result.studies.append(study.model_copy(update={"samples": samples, "reagents": reagents}))
    for n, item in enumerate(extraction.observations):
        value = normalize_text(item.value_text)
        if not located(item.locator, pages):
            result.rejected.append(f"observation {n}: quote not on page {item.locator.page}")
        elif (
            not value or len(value) > VALUE_CHARS or value not in normalize_text(item.locator.quote)
        ):
            result.rejected.append(f"observation {n}: value not in its quote")
        else:
            index = kept.get(item.study) if item.study is not None else None
            result.observations.append(
                GroundedObservation(
                    item.model_copy(update={"study": index, "value_text": value}),
                    parse_value(value),
                )
            )
    return result
