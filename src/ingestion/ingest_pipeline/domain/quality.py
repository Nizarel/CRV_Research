"""Selective-extraction gate: decide from a stored DI layout result whether a unit needs CU."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .routing import EscalationRule


@dataclass(frozen=True)
class UnitQuality:
    pages: int
    words: int
    mean_word_confidence: float | None
    text: str

    @property
    def words_per_page(self) -> float:
        return self.words / max(self.pages, 1)


def measure_layout(analyze: Mapping[str, Any]) -> UnitQuality:
    """Word count, mean OCR word confidence and text of a DI ``analyzeResult``."""

    pages = analyze.get("pages") or []
    words = [word for page in pages for word in page.get("words") or []]
    confidences = [
        float(word["confidence"]) for word in words if word.get("confidence") is not None
    ]
    return UnitQuality(
        pages=max(len(pages), 1),
        words=len(words),
        mean_word_confidence=sum(confidences) / len(confidences) if confidences else None,
        text=str(analyze.get("content") or ""),
    )


def failed_rule(
    quality: UnitQuality, unit_kind: str, rules: Sequence[EscalationRule]
) -> EscalationRule | None:
    """The first rule for this unit kind whose gate the DI result does not pass."""

    for rule in rules:
        if unit_kind not in {kind.value for kind in rule.unit_kinds}:
            continue
        if quality.words_per_page < rule.min_words_per_page:
            return rule
        confidence = quality.mean_word_confidence
        if confidence is not None and confidence < rule.min_word_confidence:
            return rule
        if rule.identifier_pattern and not re.search(rule.identifier_pattern, quality.text):
            return rule
    return None
