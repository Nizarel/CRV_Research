"""Native PDF access through pypdfium2 (Apache-2.0/BSD-3).

PDFium is not thread-safe: every call runs on one dedicated thread per process. Use more worker
processes, not threads, to parallelize.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from ...domain.errors import UnsupportedContentError
from ...domain.routing import PageSignals
from ...ports.parsing import PdfPageText, PdfProfile

T = TypeVar("T")

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdfium")
OBJECT_COUNT_CAP = 500
"""Stop counting page objects early: routing only needs zero / non-zero / many."""

# pdfium FPDF_ERR_* codes for encrypted or unsupported security handlers.
_PASSWORD_ERRORS = frozenset({4, 5})


async def _run(function: Callable[[], T]) -> T:
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, function)


def _open(path: Path) -> Any:
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as error:
        if getattr(error, "err_code", None) in _PASSWORD_ERRORS:
            raise UnsupportedContentError("PDF is encrypted or password protected") from error
        raise UnsupportedContentError(f"PDF cannot be opened: {error}") from error


def _garbage_ratio(text: str) -> float:
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return 0.0
    bad = sum(
        char == "\ufffd"
        or "\ue000" <= char <= "\uf8ff"
        or unicodedata.category(char) in ("Cc", "Co", "Cn")
        for char in visible
    )
    return bad / len(visible)


def _object_counts(page: Any) -> tuple[int, int]:
    import pypdfium2.raw as pdfium_c

    images = paths = 0
    for obj in page.get_objects(max_depth=3):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            images += 1
        elif obj.type in (pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_SHADING):
            paths += 1
        if images + paths >= OBJECT_COUNT_CAP:
            break
    return images, paths


def _page_text(page: Any) -> str:
    textpage = page.get_textpage()
    try:
        return str(textpage.get_text_bounded())
    finally:
        textpage.close()


def _label(pdf: Any, index: int) -> str | None:
    try:
        label = pdf.get_page_label(index)
    except Exception:
        return None
    return str(label) or None


def _multiple_columns(page: Any) -> bool:
    width, _ = page.get_size()
    textpage = page.get_textpage()
    try:
        left = right = 0
        for index in range(min(textpage.count_rects(), 2000)):
            x0, _, x1, _ = textpage.get_rect(index)
            if 20 < x1 - x0 < width * 0.48:
                left += int(x1 <= width * 0.53)
                right += int(x0 >= width * 0.47)
        return min(left, right) >= 8
    finally:
        textpage.close()


def _profile(path: Path) -> PdfProfile:
    pdf = _open(path)
    try:
        pages: list[PageSignals] = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                text = _page_text(page)
                images, paths = _object_counts(page)
                pages.append(
                    PageSignals(
                        page=index + 1,
                        width_pt=round(float(width), 2),
                        height_pt=round(float(height), 2),
                        rotation=int(page.get_rotation()),
                        text_chars=sum(not char.isspace() for char in text),
                        garbage_ratio=round(_garbage_ratio(text), 4),
                        image_objects=images,
                        path_objects=paths,
                        label=_label(pdf, index),
                        multi_column=_multiple_columns(page),
                    )
                )
            finally:
                page.close()
        return PdfProfile(page_count=len(pages), pages=tuple(pages))
    finally:
        pdf.close()


def _page_texts(path: Path, pages: Sequence[int]) -> list[PdfPageText]:
    pdf = _open(path)
    try:
        total = len(pdf)
        results: list[PdfPageText] = []
        for number in pages:
            if not 1 <= number <= total:
                raise UnsupportedContentError(f"Page {number} is outside 1..{total}")
            page = pdf[number - 1]
            try:
                width, height = page.get_size()
                results.append(
                    PdfPageText(
                        page=number,
                        text=_page_text(page),
                        width_pt=float(width),
                        height_pt=float(height),
                        rotation=int(page.get_rotation()),
                        label=_label(pdf, number - 1),
                    )
                )
            finally:
                page.close()
        return results
    finally:
        pdf.close()


def _write_subset(path: Path, pages: Sequence[int], destination: Path) -> int:
    import pypdfium2 as pdfium

    source = _open(path)
    subset = pdfium.PdfDocument.new()
    try:
        subset.import_pages(source, [number - 1 for number in pages])
        subset.save(str(destination))
    finally:
        subset.close()
        source.close()
    return destination.stat().st_size


class PdfiumToolkit:
    """``PdfToolkit`` over pypdfium2."""

    async def profile(self, path: Path) -> PdfProfile:
        return await _run(lambda: _profile(path))

    async def page_texts(self, path: Path, pages: Sequence[int]) -> list[PdfPageText]:
        return await _run(lambda: _page_texts(path, list(pages)))

    async def write_subset(self, path: Path, pages: Sequence[int], destination: Path) -> int:
        return await _run(lambda: _write_subset(path, list(pages), destination))
