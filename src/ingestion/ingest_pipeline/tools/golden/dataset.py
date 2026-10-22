"""Golden questions: categories, atomic key facts and their deterministic anchors.

Anchors are what a fact cannot be stated without: numbers (as printed, decimal comma or point,
grouped thousands), report codes and mixed identifiers (``KM5962``, ``Na2S2O5``, ``NP107``),
acronyms (``ACORGA``, ``QEMSCAN``) and proper names (``Box-Behnken``, ``Klimpel``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ...domain.crv import REPORT_CODE, report_codes
from ...domain.crv_kg.vocab import fold

# Numbering: Q n = Excel row - 1 (Docs/AGENTIC_ARCHITECTURE.md §1.1).
CATEGORIES: dict[str, tuple[tuple[int, int], ...]] = {
    "A": ((1, 30), (71, 76)),
    "B": ((31, 43), (53, 53)),
    "C": ((44, 44), (46, 51), (67, 69)),
    "D": ((45, 45), (54, 54), (57, 59), (66, 66)),
    "E": ((52, 52), (55, 56), (70, 70)),
    "F": ((60, 63),),
    "G": ((64, 65),),
}
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:\.\d+)?(?![\w])")
_GROUPED = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_IDENTIFIER = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{3,}\b")
_ACRONYM = re.compile(r"\b[A-Z][A-Z]{2,}\b")
_NAME = re.compile(r"(?<!^)\b[A-Z][a-zà-ÿ]{2,}(?:-[A-Z][a-zà-ÿ]+)*\b")
_STOP_NAMES = frozenset(fold(word) for word in (
    "Campagne", "Version", "Labo", "Lixiviation", "Flottation", "Pilotage", "Structure",
    "Centrale", "Sulfuration", "Collecteurs", "Arrivage", "Diagnostic", "Objectif",
    "Exemple", "Usine", "Production", "Stock", "Machine", "Learning", "Non", "Oui", "Aucun",
    "Information", "Premiers", "Introduction", "Rafraichissement", "Simulation", "Perte",
    "Pluies", "Finesse", "Proportion", "Maitrise", "Echantillon", "Divergence", "Alimentation",
    "Cours", "Etude", "Sur", "Pour", "Avec", "Dans", "Chine", "Methodologie",
))
_STOP_ACRONYMS = frozenset({"TV", "HT", "USD", "BFS", "PFS", "OCR", "CRV", "LME"})
_TRIVIAL_NUMBERS = frozenset(str(n) for n in range(0, 10))
_EXTENSION = re.compile(r"\.(pdf|docx?|pptx?|xlsx?)$", re.I)


class KeyFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    anchors: tuple[str, ...] = ()


class GoldenQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    number: int
    question: str
    expected: str
    category: str
    key_facts: tuple[KeyFact, ...]
    must_abstain: bool = False
    must_flag_conflict: bool = False
    expected_sources: tuple[str, ...] = ()
    split: str = Field(pattern=r"^(dev|heldout)$")


def category_of(number: int) -> str:
    for name, ranges in CATEGORIES.items():
        if any(low <= number <= high for low, high in ranges):
            return name
    raise ValueError(f"Q{number} is not in any category range")


def number_text(text: str) -> str:
    """Lower-case text with grouped thousands joined and decimal commas as points."""

    normalized = unicodedata.normalize("NFKC", text)
    return _DECIMAL_COMMA.sub(".", _GROUPED.sub("", normalized)).lower()


def anchors_of(fact: str, *, common: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """``common``: folded words the golden set also writes in lower case (not names)."""

    found: list[str] = []
    codes = [_EXTENSION.sub("", code) for code in report_codes(fact)]
    # Numbers inside a report code are part of the code, not quantities.
    numbers = number_text(REPORT_CODE.sub(" ", fact))
    found.extend(
        f"#{value}" for value in _NUMBER.findall(numbers)
        if value not in _TRIVIAL_NUMBERS or "." in value
    )
    found.extend(fold(code) for code in codes)
    found.extend(fold(token) for token in _IDENTIFIER.findall(REPORT_CODE.sub(" ", fact))
                 if not token.isdigit())
    found.extend(fold(token) for token in _ACRONYM.findall(fact) if token not in _STOP_ACRONYMS)
    found.extend(fold(token) for token in _NAME.findall(fact.strip())
                 if fold(token) not in _STOP_NAMES and fold(token) not in common)
    return tuple(dict.fromkeys(anchor for anchor in found if anchor and anchor != "#"))


def lowercase_words(texts: Iterable[str]) -> frozenset[str]:
    """Words written in lower case somewhere: capitalised elsewhere, they are not names."""

    return frozenset(
        fold(word) for text in texts for word in re.findall(r"\b[a-zà-ÿ][\w-]{3,}\b", text)
    )


def split_facts(expected: str) -> list[str]:
    """Atomic facts: the expected answer's ``;`` clauses (the sheet's own separator)."""

    parts = [part.strip(" .") for part in re.split(r"\s*;\s*", expected or "")]
    return [part for part in parts if len(part) >= 3]


def build_questions(rows: Iterable[tuple[str, str]]) -> list[GoldenQuestion]:
    pairs = list(rows)
    common = lowercase_words(text for pair in pairs for text in pair)
    questions: list[GoldenQuestion] = []
    seen: dict[str, int] = {}
    for number, (question, expected) in enumerate(pairs, start=1):
        category = category_of(number)
        position = seen.get(category, 0)
        seen[category] = position + 1
        facts = tuple(KeyFact(text=text, anchors=anchors_of(text, common=common))
                      for text in split_facts(expected))
        questions.append(GoldenQuestion(
            id=f"Q{number}",
            number=number,
            question=question.strip(),
            expected=expected.strip(),
            category=category,
            key_facts=facts,
            must_abstain=category == "F",
            must_flag_conflict=category == "D",
            # Stratified: every third question of a category is held out.
            split="heldout" if position % 3 == 2 else "dev",
        ))
    return questions


class AnchorText:
    """One retrieved text prepared once for anchor lookups."""

    def __init__(self, text: str) -> None:
        self.numbers = number_text(text)
        self.words = f" {fold(text)} "

    def has(self, anchor: str) -> bool:
        if anchor.startswith("#"):
            value = re.escape(anchor[1:])
            return re.search(rf"(?<![\d.]){value}(?!\.?\d)", self.numbers) is not None
        return f" {anchor} " in self.words


def anchor_hits(anchors: Sequence[str], texts: Sequence[AnchorText]) -> set[str]:
    return {anchor for anchor in anchors if any(text.has(anchor) for text in texts)}
