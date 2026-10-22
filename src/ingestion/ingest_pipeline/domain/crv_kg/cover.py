"""Fields of the CRV report cover (``Templates des Livrables/Rapport CRV.pdf``).

The cover prints ``Label : value`` lines (Emetteur, Projet, Titre, Objet, Domaine
d'application, Attribué à, Mots-clés) and a revision block (index, author, verifier, approver,
dates). Document Intelligence keeps them as page-1 paragraphs or table rows. Values are kept
exactly as printed. People are never captured: the revision block yields only its index and
dates, and ``Attribué à`` / ``Copié à`` values are not stored (they often name a person).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date

from .vocab import MONTHS, fold

MAX_VALUE_CHARS = 300
_LABELS = (
    ("emetteur", "issuer"),
    ("projet", "project"),
    ("titre", "title"),
    ("objet", "subject"),
    ("domaine d application", "scope"),
    ("attribue a", "attributed_to"),
    ("mots cles", "keywords"),
    ("copie a", "copied_to"),
    ("textes de reference", "references"),
)
_LABEL = re.compile(
    r"^(" + "|".join(re.escape(label) for label, _ in _LABELS) + r")$"
)
_DATE = re.compile(
    r"\bdate\s*[:\uff1a]?\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*((?:19|20)?\d{2})\b", re.I
)
_REVISION_HEADER = re.compile(r"^r[ée]vision\b.*r[ée]daction", re.I)
_REVISION_INDEX = re.compile(r"^\s*(\d{1,2}|[A-Z])\b")
_PERIOD = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s+((?:19|20)\d{2})\b"
)
STORED_FIELDS = frozenset({"issuer", "project", "title", "subject", "scope", "keywords"})


@dataclass(frozen=True)
class CoverLine:
    text: str
    page: int
    evidence_id: str


@dataclass(frozen=True)
class CoverValue:
    value: str
    page: int
    evidence_id: str


@dataclass(frozen=True)
class CoverFacts:
    fields: dict[str, CoverValue] = field(default_factory=dict)
    """Stored fields only (issuer, project, title, subject, scope, keywords)."""
    attributed_to: CoverValue | None = None
    """Kept in memory to resolve organisations; never stored as text."""
    dates: tuple[tuple[date, CoverValue], ...] = ()
    revision: CoverValue | None = None
    period: tuple[int, int, CoverValue] | None = None
    """First ``<month name> <year>`` printed on page 1 (``Décembre 2021``): year, month."""

    @property
    def latest_date(self) -> tuple[date, CoverValue] | None:
        return max(self.dates, key=lambda item: item[0]) if self.dates else None


def _label_of(line: str) -> tuple[str, str] | None:
    """``(field, printed value)`` for a ``Label : value`` line; the colon is required, so body
    sentences such as "Projet de valorisation..." are not mistaken for cover fields."""

    head, separator, tail = line.replace("\uff1a", ":").partition(":")
    if not separator:
        return None
    match = _LABEL.match(fold(head))
    if not match or fold(head) != match.group(1):
        return None
    return dict(_LABELS)[match.group(1)], tail.strip()


def parse_cover(lines: Iterable[CoverLine]) -> CoverFacts:
    ordered = list(lines)
    fields: dict[str, CoverValue] = {}
    attributed: CoverValue | None = None
    dates: list[tuple[date, CoverValue]] = []
    revision: CoverValue | None = None
    period: tuple[int, int, CoverValue] | None = None
    after_revision_header = False
    for index, line in enumerate(ordered):
        text = line.text.strip()
        if not text:
            continue
        if period is None and line.page == 1 and (found_period := _PERIOD.search(fold(text))):
            period = (int(found_period.group(2)), MONTHS[found_period.group(1)],
                      CoverValue(found_period.group(0), line.page, line.evidence_id))
        for match in _DATE.finditer(text):
            day, month, year = (int(group) for group in match.groups())
            year = year + (2000 if year < 50 else 1900) if year < 100 else year
            with suppress(ValueError):
                dates.append((date(year, month, day), CoverValue(match.group(0), line.page,
                                                                 line.evidence_id)))
        if _REVISION_HEADER.match(text):
            after_revision_header = True
            continue
        if after_revision_header:
            after_revision_header = False
            if revision is None and (found := _REVISION_INDEX.match(text)):
                revision = CoverValue(found.group(1), line.page, line.evidence_id)
                continue
        labelled = _label_of(text)
        if labelled is None:
            continue
        name, value = labelled
        if not value:
            following = ordered[index + 1].text.strip() if index + 1 < len(ordered) else ""
            if following and _label_of(following) is None and not _REVISION_HEADER.match(
                following
            ) and not _DATE.search(following):
                value = following
        value = value[:MAX_VALUE_CHARS].strip()
        if not value:
            continue
        cover_value = CoverValue(value, line.page, line.evidence_id)
        if name == "attributed_to":
            attributed = attributed or cover_value
        elif name in STORED_FIELDS:
            fields.setdefault(name, cover_value)
    return CoverFacts(fields, attributed, tuple(dates), revision, period)
