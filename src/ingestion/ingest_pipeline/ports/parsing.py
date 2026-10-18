"""Native parser and tokenizer ports. Implementations live in ``adapters/parsers``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..domain.routing import PageSignals, SheetSignals


@dataclass(frozen=True)
class PdfProfile:
    page_count: int
    pages: tuple[PageSignals, ...]


@dataclass(frozen=True)
class PdfPageText:
    page: int
    """Original 1-based page index."""
    text: str
    width_pt: float
    height_pt: float
    rotation: int
    label: str | None = None


class PdfToolkit(Protocol):
    async def profile(self, path: Path) -> PdfProfile:
        """Per-page signals. Raises ``UnsupportedContentError`` for encrypted/unreadable PDFs."""
        ...

    async def page_texts(self, path: Path, pages: Sequence[int]) -> list[PdfPageText]: ...

    async def write_subset(self, path: Path, pages: Sequence[int], destination: Path) -> int:
        """Write a PDF of ``pages`` (original order) to ``destination``; return its size."""
        ...


@dataclass(frozen=True)
class SheetCell:
    row: int
    """Original 1-based sheet row."""
    column: int
    """Original 1-based sheet column."""
    text: str
    value_type: str


@dataclass(frozen=True)
class SheetData:
    sheet: SheetSignals
    cells: tuple[SheetCell, ...]
    """Non-empty cells only, row-major, with original coordinates."""
    merged: tuple[tuple[int, int, int, int], ...] = field(default=())
    """Merged ranges as original 1-based ``(first_row, first_col, last_row, last_col)``."""


class SpreadsheetReader(Protocol):
    async def profile(self, path: Path) -> list[SheetSignals]: ...

    async def read(self, path: Path, *, include_hidden: bool, max_cells: int) -> list[SheetData]:
        """Cached cell values only (formulas are never evaluated, macros never run)."""
        ...


@dataclass(frozen=True)
class DrawingText:
    layout: str
    """``Model`` or the paper-space layout name."""
    entity: str
    """DXF type: ``TEXT``, ``MTEXT``, ``DIMENSION`` (override text) or ``MULTILEADER``."""
    text: str
    x: float
    y: float
    height: float
    """Character height in drawing units; 0 when unknown."""


@dataclass(frozen=True)
class DrawingInsert:
    layout: str
    block: str
    attributes: tuple[tuple[str, str], ...]
    """``(tag, value)`` of the insert's non-empty attributes, in drawing order."""


@dataclass(frozen=True)
class DrawingData:
    converter: str
    dxf_version: str
    codepage: str | None
    layouts: tuple[str, ...]
    """Layouts holding at least one entity, model space first."""
    entities: int
    texts: tuple[DrawingText, ...]
    inserts: tuple[DrawingInsert, ...]
    solids: int
    """ACIS bodies (3D solids, regions, surfaces): geometry whose content is not read."""
    xrefs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class DrawingReader(Protocol):
    async def read(self, path: Path, work_dir: Path) -> DrawingData:
        """Convert a DWG and read its text, never its geometry.

        Raises ``UnsupportedContentError`` when the drawing cannot be converted or read, and
        ``ConfigurationError`` when the converter itself is missing or broken.
        """
        ...


class TokenCounter(Protocol):
    @property
    def name(self) -> str:
        """Recorded in chunk identities: changing the tokenizer changes chunks."""
        ...

    def count(self, text: str) -> int: ...
