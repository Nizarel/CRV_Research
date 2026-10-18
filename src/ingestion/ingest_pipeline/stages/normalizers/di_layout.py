"""Normalize a Document Intelligence v4.0 Layout result into canonical evidence.

Structure comes from the JSON elements (paragraph roles, sections, tables, figures), never from
re-parsing the Markdown. Paragraphs that belong to a table cell, caption or figure are folded
into that table or figure so text is not duplicated. Spans keep DI's own representation and
string index type; they are stored as locators, never used to slice cleaned text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...domain.errors import IntegrityError
from ...domain.evidence import (
    BlockKind,
    CellKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceFigure,
    EvidencePage,
    EvidenceTable,
    EvidenceUnit,
    Locator,
    LocatorKind,
    PageStatus,
    block_id,
    detect_language,
    normalize_text,
)
from ...domain.models import ArtifactRef, UnitRecord

NORMALIZER = "di-layout@1"
_ROLES: dict[str, BlockKind] = {
    "title": BlockKind.TITLE,
    "sectionHeading": BlockKind.HEADING,
    "pageHeader": BlockKind.PAGE_HEADER,
    "pageFooter": BlockKind.PAGE_FOOTER,
    "pageNumber": BlockKind.PAGE_NUMBER,
    "footnote": BlockKind.FOOTNOTE,
}
_CELL_KINDS = {kind.value: kind for kind in CellKind}


def _ref_index(ref: str, collection: str) -> int | None:
    prefix = f"/{collection}/"
    if ref.startswith(prefix) and ref[len(prefix) :].isdigit():
        return int(ref[len(prefix) :])
    return None


def resolve_page_map(
    di_pages: Sequence[int], unit_pages: Sequence[int], explicit: Mapping[int, int] | None
) -> dict[int, int]:
    """Map DI page numbers to original 1-based pages; refuse anything ambiguous."""

    if explicit:
        missing = [page for page in di_pages if page not in explicit]
        if missing:
            raise IntegrityError(f"DI returned pages {missing} outside the submitted subset")
        return dict(explicit)
    if not unit_pages:
        return {page: page for page in di_pages}
    if set(di_pages) <= set(unit_pages):
        return {page: page for page in di_pages}
    if list(di_pages) == list(range(1, len(di_pages) + 1)) and len(di_pages) <= len(unit_pages):
        return {position: unit_pages[position - 1] for position in di_pages}
    raise IntegrityError(f"DI pages {list(di_pages)} do not match unit pages {list(unit_pages)}")


class _Builder:
    def __init__(
        self,
        analyze: Mapping[str, Any],
        *,
        raw: ArtifactRef,
        page_map: dict[int, int],
        representation: str,
        index_type: str,
    ) -> None:
        self.analyze = analyze
        self.raw = raw
        self.page_map = page_map
        self.representation = representation
        self.index_type = index_type
        self.page_info = {
            int(page["pageNumber"]): page for page in analyze.get("pages") or []
        }
        self.blocks: list[tuple[int, EvidenceBlock]] = []

    def page(self, di_page: int) -> int:
        try:
            return self.page_map[di_page]
        except KeyError as error:
            raise IntegrityError(f"DI page {di_page} has no original page") from error

    def regions(self, element: Mapping[str, Any]) -> tuple[list[int], list[Locator]]:
        pages: list[int] = []
        locators: list[Locator] = []
        for region in element.get("boundingRegions") or []:
            di_page = int(region["pageNumber"])
            original = self.page(di_page)
            info = self.page_info.get(di_page, {})
            pages.append(original)
            polygon = tuple(float(value) for value in region.get("polygon") or ())
            if polygon:
                locators.append(
                    Locator(
                        kind=LocatorKind.REGION,
                        page=original,
                        polygon=polygon,
                        unit=info.get("unit"),
                        page_width=info.get("width"),
                        page_height=info.get("height"),
                    )
                )
            else:
                locators.append(Locator(kind=LocatorKind.PAGE, page=original))
        return sorted(set(pages)), locators

    def spans(self, element: Mapping[str, Any]) -> list[Locator]:
        return [
            Locator(
                kind=LocatorKind.SPAN,
                artifact_sha256=self.raw.sha256,
                representation=self.representation,
                index_type=self.index_type,
                offset=int(span["offset"]),
                length=int(span.get("length", 0)),
            )
            for span in element.get("spans") or []
        ]

    @staticmethod
    def offset(element: Mapping[str, Any], default: int) -> int:
        spans = element.get("spans") or []
        return min((int(span["offset"]) for span in spans), default=default)

    def add(self, order: int, kind: BlockKind, element: Mapping[str, Any], **fields: Any) -> None:
        pages, locators = self.regions(element)
        self.blocks.append(
            (
                order,
                EvidenceBlock(
                    evidence_id="pending",
                    kind=kind,
                    pages=tuple(pages),
                    locators=(*locators, *self.spans(element)),
                    **fields,
                ),
            )
        )


def _section_depths(analyze: Mapping[str, Any]) -> dict[int, int]:
    sections = analyze.get("sections") or []
    depths: dict[int, int] = {}
    if not sections:
        return depths
    stack = [(0, 0)]
    seen: set[int] = set()
    while stack:
        index, depth = stack.pop()
        if index in seen or index >= len(sections):
            continue
        seen.add(index)
        for ref in sections[index].get("elements") or []:
            child = _ref_index(ref, "sections")
            if child is not None:
                stack.append((child, depth + 1))
                continue
            paragraph = _ref_index(ref, "paragraphs")
            if paragraph is not None:
                depths.setdefault(paragraph, depth)
    return depths


def _owned_paragraphs(analyze: Mapping[str, Any]) -> dict[int, tuple[str, int]]:
    """Paragraph index -> (``table``/``figure``, owner index) for folded paragraphs."""

    owned: dict[int, tuple[str, int]] = {}
    for kind in ("tables", "figures"):
        for owner, element in enumerate(analyze.get(kind) or []):
            refs: list[str] = list(element.get("elements") or [])
            for part in ("caption",):
                refs.extend((element.get(part) or {}).get("elements") or [])
            for footnote in element.get("footnotes") or []:
                refs.extend(footnote.get("elements") or [])
            for cell in element.get("cells") or []:
                refs.extend(cell.get("elements") or [])
            for ref in refs:
                index = _ref_index(ref, "paragraphs")
                if index is not None:
                    owned.setdefault(index, (kind[:-1], owner))
    return owned


def _table(element: Mapping[str, Any], builder: _Builder) -> EvidenceTable:
    cells: list[EvidenceCell] = []
    for cell in element.get("cells") or []:
        _, locators = builder.regions(cell)
        cells.append(
            EvidenceCell(
                row=int(cell["rowIndex"]),
                column=int(cell["columnIndex"]),
                row_span=int(cell.get("rowSpan", 1) or 1),
                column_span=int(cell.get("columnSpan", 1) or 1),
                kind=_CELL_KINDS.get(str(cell.get("kind", "content")), CellKind.CONTENT),
                text=normalize_text(str(cell.get("content", ""))),
                locator=locators[0] if locators else None,
            )
        )
    rows = int(element.get("rowCount", 0))
    header_rows = 0
    for row in range(rows):
        in_row = [cell for cell in cells if cell.row == row]
        if in_row and all(cell.kind is CellKind.COLUMN_HEADER for cell in in_row):
            header_rows = row + 1
        else:
            break
    caption = (element.get("caption") or {}).get("content")
    return EvidenceTable(
        row_count=rows,
        column_count=int(element.get("columnCount", 0)),
        cells=tuple(cells),
        header_rows=header_rows,
        caption=normalize_text(caption) if caption else None,
        footnotes=tuple(
            normalize_text(str(note.get("content", "")))
            for note in element.get("footnotes") or []
        ),
    )


def normalize_layout(
    document: Mapping[str, Any],
    *,
    unit: UnitRecord,
    plan_id: str,
    content_hash: str,
    raw: ArtifactRef,
    page_map: Mapping[int, int] | None,
    request: Mapping[str, Any],
) -> EvidenceUnit:
    """``document`` is the full GET response (``status`` + ``analyzeResult``)."""

    analyze = document.get("analyzeResult")
    if not isinstance(analyze, Mapping):
        raise IntegrityError("DI response has no analyzeResult")
    di_pages = [int(page["pageNumber"]) for page in analyze.get("pages") or []]
    mapping = resolve_page_map(di_pages, unit.pages, page_map)
    index_type = str(analyze.get("stringIndexType", "textElements"))
    ordered, pages = layout_blocks(
        analyze,
        raw=raw,
        mapping=mapping,
        representation=f"di_{analyze.get('contentFormat', 'text')}",
        index_type=index_type,
    )
    blocks = tuple(
        block.model_copy(update={"evidence_id": block_id(unit.unit_id, raw.sha256, position)})
        for position, (_, block) in enumerate(sorted(ordered, key=lambda item: item[0]))
    )
    text = "\n".join(block.text for block in blocks if block.text)
    return EvidenceUnit(
        unit_id=unit.unit_id,
        plan_id=plan_id,
        content_hash=content_hash,
        route=unit.route,
        extraction_method="di_layout",
        normalizer=NORMALIZER,
        provider={
            "api_version": analyze.get("apiVersion"),
            "model_id": analyze.get("modelId"),
            "content_format": analyze.get("contentFormat"),
            "string_index_type": index_type,
            "request": dict(request),
            "unit_kind": unit.kind,
        },
        raw_artifact=raw,
        pages=tuple(sorted(pages, key=lambda page: page.page)),
        blocks=blocks,
        language=detect_language(text),
        warnings=tuple(
            str(warning.get("code", "warning")) for warning in analyze.get("warnings") or []
        ),
    )


def layout_blocks(
    analyze: Mapping[str, Any],
    *,
    raw: ArtifactRef,
    mapping: dict[int, int],
    representation: str,
    index_type: str,
) -> tuple[list[tuple[int, EvidenceBlock]], list[EvidencePage]]:
    """Blocks (with their reading-order key, IDs still pending) and pages of a layout result."""

    builder = _Builder(
        analyze, raw=raw, page_map=mapping, representation=representation, index_type=index_type
    )
    paragraphs = analyze.get("paragraphs") or []
    depths = _section_depths(analyze)
    owned = _owned_paragraphs(analyze)

    for index, paragraph in enumerate(paragraphs):
        if index in owned:
            continue
        text = normalize_text(str(paragraph.get("content", "")))
        if not text:
            continue
        kind = _ROLES.get(str(paragraph.get("role")), BlockKind.PARAGRAPH)
        level = None
        if kind is BlockKind.TITLE:
            level = 1
        elif kind is BlockKind.HEADING:
            level = min(max(depths.get(index, 1), 1) + 1, 9)
        builder.add(
            builder.offset(paragraph, index),
            kind,
            paragraph,
            text=text,
            level=level,
            role=paragraph.get("role"),
        )

    for index, table in enumerate(analyze.get("tables") or []):
        evidence_table = _table(table, builder)
        builder.add(
            builder.offset(table, len(paragraphs) + index),
            BlockKind.TABLE,
            table,
            text=evidence_table.caption or "",
            table=evidence_table,
        )

    for index, figure in enumerate(analyze.get("figures") or []):
        caption = (figure.get("caption") or {}).get("content")
        caption_refs = {
            _ref_index(ref, "paragraphs")
            for ref in (figure.get("caption") or {}).get("elements") or []
        }
        inner = [
            normalize_text(str(paragraphs[p].get("content", "")))
            for p, (kind, owner) in sorted(owned.items())
            if kind == "figure"
            and owner == index
            and p < len(paragraphs)
            and p not in caption_refs
        ]
        text = "\n".join(part for part in [normalize_text(caption or ""), *inner] if part)
        builder.add(
            builder.offset(figure, len(paragraphs) + index),
            BlockKind.FIGURE,
            figure,
            text=text,
            figure=EvidenceFigure(
                figure_id=str(figure.get("id", index)),
                caption=normalize_text(caption) if caption else None,
            ),
        )

    pages = []
    for di_page in sorted(mapping):
        info = builder.page_info.get(di_page, {})
        has_text = bool(info.get("words") or info.get("lines"))
        pages.append(
            EvidencePage(
                page=mapping[di_page],
                width=info.get("width"),
                height=info.get("height"),
                unit=info.get("unit"),
                rotation=info.get("angle"),
                status=PageStatus.CONTENT if has_text else PageStatus.NO_TEXT,
                reason=None if has_text else "layout analysis found no text",
            )
        )
    return builder.blocks, pages
