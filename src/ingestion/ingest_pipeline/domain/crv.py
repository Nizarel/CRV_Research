"""CRV research context; path labels are reported context, not reviewed scientific facts."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

REPORT_CODE = re.compile(r"\bCR(?:[-/][A-Z0-9]+(?:\.[A-Z0-9]+)*){2,7}\b", re.IGNORECASE)
# OCR reads the first "/" of a running-header code as a letter: CRIAR/PE/... is CR/AR/PE/...
_OCR_FIRST_SLASH = re.compile(r"\bCR[I1l|](?=[A-Z]{2}/)")


def report_codes(text: str) -> list[str]:
    """CRV report codes in printed text, upper-cased, with the known OCR slash misread repaired."""

    repaired = _OCR_FIRST_SLASH.sub("CR/", text)
    return list(dict.fromkeys(code.upper() for code in REPORT_CODE.findall(repaired)))


def label(value: str) -> str:
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(c)
        ).replace("_", " ").split()
    )


SITES = {
    "akka - agm": "agm", "agm": "agm", "bleida": "bleida", "bou-azzer": "bou-azzer",
    "bou azzer divers": "bou-azzer", "bouazzer": "bou-azzer", "bouskour": "bouskour",
    "eie bouskour": "bouskour", "cmg": "cmg", "eie hajjar": "hajjar",
    "ctt hydro": "ctt", "oumjrane": "oumjrane", "cmo": "oumjrane",
    "samine": "samine", "smi": "smi", "eie tizert": "tizert", "tizert": "tizert",
    "eie trik": "tri-k", "eie tri-k": "tri-k", "tri-k": "tri-k",
    "ouansimi": "ouansimi", "somifer": "somifer", "maa": "maa",
}

_CLASSES = (
    (r"templates|rapport crv\.pdf|presentation reminex\.pdf", "template"),
    (r"reglementation|\bloi[_ -]|\bdecret", "regulation"),
    (r"procedure|instruction", "procedure"),
    (r"hsc doc", "software_manual"),
    (r"\blims\b|offre technique|offre commerciale|subscription order|\bsow\b", "commercial"),
    (r"bibliotheque|et al|\bdoi\b", "scientific_reference"),
    (r"\beie\b|environnement|phytoremediation", "environmental_study"),
    (r"essai|pilotage|caracterisation|mineralogique", "test_report"),
    (r"rapport|documents r&d|documents activite|geometallurgy", "research_report"),
    (r"veille|benchmark", "research_reference"),
)


@dataclass(frozen=True)
class ResearchContext:
    collection: str
    site_ids: tuple[str, ...]
    document_type: str
    source_path: str
    file_name: str
    report_codes: tuple[str, ...]


def research_context(path: str) -> ResearchContext:
    parts = path.replace("\\", "/").split("/")
    normalized = [label(part) for part in parts]
    joined = " / ".join(normalized)
    collection = next(
        (
            name for marker, name in (
                ("lims", "lims-commercial"),
                ("phytoremediation", "environment"),
                ("geometallurgy", "geometallurgy"),
                ("hsc doc", "reference"),
                ("bibliotheque", "reference"),
            ) if marker in joined
        ),
        "research",
    )
    kind = next(
        (name for pattern, name in _CLASSES if re.search(pattern, joined)), "unclassified"
    )
    sites = tuple(dict.fromkeys(SITES[p] for p in normalized[:-1] if p in SITES))
    return ResearchContext(
        collection, sites, kind, "/".join(parts), parts[-1], tuple(report_codes(path)),
    )


def unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
