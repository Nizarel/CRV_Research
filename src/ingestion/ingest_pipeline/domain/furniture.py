"""Document-level page furniture and the document's own printed codes.

Layout models do not always label running headers as page furniture: a repeated header can come
back as a paragraph or even a section heading, which then pollutes chunk text and section
paths. A short line repeated across a large share of a document's pages is furniture whatever
role the model gave it. Report codes printed in headers are the document's own identity, unlike
codes cited in the body, so they are collected here before the furniture is dropped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .crv import report_codes
from .evidence import FURNITURE, BlockKind, EvidenceUnit

MAX_FURNITURE_CHARS = 150
MIN_RUNNING_PAGES = 3
MIN_RUNNING_SHARE = 0.3
_CANDIDATES = frozenset({BlockKind.PARAGRAPH, BlockKind.HEADING, BlockKind.TITLE})
REMOVABLE = frozenset({BlockKind.PARAGRAPH, BlockKind.HEADING})
"""Kinds dropped when repeated; a document title is kept even when a header repeats it."""


def furniture_key(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass
class FurnitureScan:
    """Feed every unit of one document, then read ``running`` and ``document_codes``."""

    _line_pages: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    _code_pages: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    _header_codes: Counter[str] = field(default_factory=Counter)
    _pages: set[int] = field(default_factory=set)

    def add(self, unit: EvidenceUnit) -> None:
        for block in unit.blocks:
            pages = set(block.pages)
            self._pages |= pages
            texts = [block.text]
            if block.table is not None:
                texts += [cell.text for cell in block.table.cells]
            for code in report_codes(" ".join(texts)):
                self._code_pages[code] |= pages
                if block.kind in FURNITURE:
                    self._header_codes[code] += len(pages) or 1
            if block.kind in _CANDIDATES and 0 < len(block.text) <= MAX_FURNITURE_CHARS:
                self._line_pages[furniture_key(block.text)] |= pages

    def _threshold(self) -> int:
        return max(MIN_RUNNING_PAGES, round(MIN_RUNNING_SHARE * len(self._pages)))

    def running(self) -> frozenset[str]:
        threshold = self._threshold()
        return frozenset(
            key for key, pages in self._line_pages.items() if len(pages) >= threshold
        )

    def document_codes(self) -> tuple[str, ...]:
        """Codes printed as page furniture or repeated like it, most pages first."""

        threshold = self._threshold()
        ranked: Counter[str] = Counter(self._header_codes)
        for code, pages in self._code_pages.items():
            if len(pages) >= threshold:
                ranked[code] += len(pages)
        return tuple(
            code for code, _ in sorted(ranked.items(), key=lambda item: (-item[1], item[0]))
        )
