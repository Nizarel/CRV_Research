"""Classes, relations, statuses, scopes and genre vocabulary of the KG-1 slice."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

TOOL = "crv-kg-build"
TOOL_VERSION = "2"
"""2: periodic-summary genres (activity-report, executive-summary, flash, project-reporting)."""
PROJECTION_VERSION = "crv-kg-projection/1"

ENTITY_KINDS = frozenset(
    {
        "Document",
        "VersionFamily",
        "ReportCode",
        "Organisation",
        "Site",
        "Project",
        "Collection",
        "Folder",
    }
)
EDGE_LABELS: Mapping[str, str] = {
    "hasCode": "HAS_CODE",
    "inCollection": "IN_COLLECTION",
    "filedUnder": "FILED_UNDER",
    "aboutSite": "ABOUT_SITE",
    "concerns": "CONCERNS",
    "issuedBy": "ISSUED_BY",
    "attributedTo": "ATTRIBUTED_TO",
    "aboutProject": "ABOUT_PROJECT",
    "memberOf": "MEMBER_OF",
    "supersedes": "SUPERSEDES",
    "duplicateOf": "DUPLICATE_OF",
    "partOf": "PART_OF",
    "operatedBy": "OPERATED_BY",
}
METHODS = frozenset(
    {"path", "filename", "cover", "header_code", "printed_code", "title", "revision", "gazetteer"}
)
STATUSES = frozenset({"observed", "candidate", "approved", "rejected", "superseded"})
# Higher wins when several assertions back one edge.
STATUS_RANK: Mapping[str, int] = {
    "rejected": 0,
    "superseded": 0,
    "candidate": 1,
    "observed": 2,
    "approved": 3,
}
DEFAULT_RELEASE_STATUSES = ("approved", "candidate", "observed")
"""Candidates are released flagged (``status`` on every vertex and edge), per the design."""

COLLECTION_SCOPES: Mapping[str, str] = {
    "research": "crv:research",
    "geometallurgy": "crv:geometallurgy",
    "environment": "crv:environment",
    "reference": "crv:reference",
}
GAZETTEER_SCOPE = "crv:gazetteer"
EXCLUDED_COLLECTIONS = frozenset({"lims-commercial", "reference"})
"""Commercial LIMS documents never contribute to the shared graph; HSC manuals and the digital
library (textbooks) are search-only (design §1.2)."""

MONTHS: Mapping[str, int] = {
    **{name: number for number, name in enumerate(
        ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre",
         "octobre", "novembre", "decembre"), start=1)},
    **{name: number for number, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"), start=1)},
    "janv": 1, "fev": 2, "fevr": 2, "avr": 4, "juil": 7, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12,
}
"""Folded French and English month names (and the usual abbreviations)."""

# First word of a file name (folded) -> genre concept.
GENRE_WORDS: Mapping[str, str] = {
    "rapport": "report",
    "report": "report",
    "pv": "minutes",
    "proces": "minutes",
    "compte": "minutes",
    "reunion": "minutes",
    "etude": "study",
    "executive": "executive-summary",
    "synthese": "synthesis-report",
    "proposition": "proposal",
    "offre": "offer",
    "revue": "review",
    "bilan": "assessment",
    "evaluation": "assessment",
    "diagnostic": "diagnostic",
    "pilotage": "pilot-report",
    "caracterisation": "characterisation",
    "mission": "mission-report",
    "prediction": "prediction",
    "flash": "flash",
    "veille": "watch",
    "benchmark": "watch",
    "presentation": "presentation",
    "chapter": "chapter",
    "chapitre": "chapter",
    "annexure": "annex",
    "annexe": "annex",
    "annex": "annex",
    "resultats": "results",
    "procedure": "procedure",
    "instruction": "procedure",
    "note": "note",
    "plan": "plan",
    "programme": "plan",
    "cahier": "specification",
    "fiche": "datasheet",
    "manuel": "manual",
    "guide": "manual",
    # Titles that open on the investigated process are study reports.
    "valorisation": "study",
    "traitement": "study",
    "developpement": "study",
    "amelioration": "study",
    "optimisation": "study",
    "essais": "study",
    "essai": "study",
    "recuperation": "study",
    "lixiviation": "study",
    "flottation": "study",
    "accompagnement": "study",
    "appui": "study",
}
# Report-code document types -> genre.
CODE_TYPE_GENRES: Mapping[str, str] = {
    "RP": "report",
    "PV": "minutes",
    "I": "procedure",
    "P": "procedure",
    "PR": "procedure",
    "NT": "note",
}
# Path-derived document type (domain/crv.py) -> genre, the last resort.
DOCUMENT_TYPE_GENRES: Mapping[str, str] = {
    "research_report": "report",
    "test_report": "report",
    "environmental_study": "environmental-study",
    "scientific_reference": "reference",
    "research_reference": "watch",
    "software_manual": "manual",
    "procedure": "procedure",
    "regulation": "regulation",
    "template": "template",
    "commercial": "commercial",
}


def fold(value: str) -> str:
    """Lower-case ASCII words separated by single spaces (accents folded, punctuation gone)."""

    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


# Periodic summaries restate many studies briefly; retrieval should prefer the primary reports
# (Docs/evaluation/golden-retrieval-baseline-2026-10-21.md). Matched on the folded title.
_PERIODIC: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brapport d activite\b|\brapport mensuel\b|\bmonthly report\b|^ra\b"),
     "activity-report"),
    (re.compile(r"\bexecutive summary\b"), "executive-summary"),
    (re.compile(r"\bflash\b"), "flash"),
    (re.compile(r"^reporting\b"), "project-reporting"),
)
PERIODIC_GENRES = frozenset(genre for _, genre in _PERIODIC)


def periodic_genre(folded_title: str) -> str | None:
    """Genre of a periodic summary (monthly activity report, executive summary, weekly flash,
    project reporting), or None for a primary document."""

    return next((genre for pattern, genre in _PERIODIC if pattern.search(folded_title)), None)


def slug(value: str, *, limit: int = 80) -> str:
    return fold(value).replace(" ", "-")[:limit].strip("-") or "x"
