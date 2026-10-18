"""Evidence from a DWG's text: block attributes, text rebuilt into rows, frame text set aside.

Text entities are scattered on a sheet; grouping them by baseline and ordering them by x turns
a drawn table (bill of materials, revision list) back into readable rows. Text repeated on every
sheet of a fabricator's frame is kept as page furniture: stored as evidence, never chunked.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterator, Sequence

from ...domain.evidence import (
    BlockKind,
    CellKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceKind,
    EvidenceTable,
    EvidenceUnit,
    FieldObservation,
    Locator,
    LocatorKind,
)
from ...domain.ids import sha256_hex
from ...domain.models import UnitRecord
from ...ports.parsing import DrawingData, DrawingInsert, DrawingText
from .ifc import DOCUMENT, TAG
from .native import _unit

NATIVE_DWG = "native-dwg@1"
TITLE_BLOCK_MIN_ATTRIBUTES = 4
MAX_TABLE_ROWS = 200
MAX_CHARS = 100_000
MIN_CHARS = 20
"""Below this, a drawing without attributes is lineage only (sheet sizes, stray digits)."""

FRAME_TEXT = frozenset(
    {
        "building 12",
        "greenstone hill office park",
        "emerald boulevard",
        "greenstone, 1609.",
        "(modderfontein)",
        "republic of south africa",
        "tel: (int.) 27 11 409 1300",
        "fax: (int.) 27 11 409 1301",
        "email: drawingoffice@senet.co.za",
        "general notes: 1. all holes ø22 u.n.o. 2. all welds to be 6mm c.f.w u.n.o. "
        "3. do not scale this drawing.",
        "métallurgie de l'anti-atlas sp0904 - tizert copper project",
        "métallurgie de l'anti-atlas tizert copper project",
    }
)
"""Fabricator frame repeated on every shop drawing of the 2026-10-09 pilot (174 of 174)."""


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _sheet(layout: str) -> Locator:
    return Locator(kind=LocatorKind.DOCUMENT, sheet=layout)


def rows(texts: Sequence[DrawingText]) -> list[str]:
    """Text lines in reading order: same baseline joins a row, wide gaps become `` | ``."""

    heights = [text.height for text in texts if text.height > 0]
    typical = statistics.median(heights) if heights else 1.0
    lines: list[list[DrawingText]] = []
    for text in sorted(texts, key=lambda item: (-item.y, item.x)):
        if lines:
            anchor = lines[-1][0]
            tolerance = 0.5 * max(anchor.height or typical, text.height or typical)
            if abs(anchor.y - text.y) <= tolerance:
                lines[-1].append(text)
                continue
        lines.append([text])
    result: list[str] = []
    for line in lines:
        line.sort(key=lambda item: item.x)
        parts = [line[0].text]
        end = line[0].x + _width(line[0], typical)
        for text in line[1:]:
            near = text.x - end <= 1.5 * (text.height or typical)
            parts.append((" " if near else " | ") + text.text)
            end = max(end, text.x + _width(text, typical))
        result.append("".join(parts))
    return result


def _width(text: DrawingText, typical: float) -> float:
    # No font metrics: an average glyph is about 0.7 of the character height.
    return 0.7 * (text.height or typical) * len(text.text)


def _attribute_blocks(
    inserts: Sequence[DrawingInsert], layout: str
) -> tuple[list[EvidenceBlock], list[DrawingInsert], list[str]]:
    """Single inserts become paragraphs; repeated blocks become one de-duplicated table."""

    blocks: list[EvidenceBlock] = []
    title_blocks: list[DrawingInsert] = []
    warnings: list[str] = []
    by_name: dict[str, list[DrawingInsert]] = {}
    for insert in inserts:
        by_name.setdefault(insert.block, []).append(insert)
    for name, group in by_name.items():
        if len(group) == 1:
            insert = group[0]
            pairs = "; ".join(f"{tag}: {value}" for tag, value in insert.attributes)
            title = len(insert.attributes) >= TITLE_BLOCK_MIN_ATTRIBUTES
            if title:
                title_blocks.append(insert)
            label = "Title block" if title else "Block"
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.PARAGRAPH,
                    text=f"{label} {name}: {pairs}",
                    locators=(_sheet(layout),),
                )
            )
            continue
        tags = list(dict.fromkeys(tag for insert in group for tag, _ in insert.attributes))
        counts = Counter(
            tuple(dict(insert.attributes).get(tag, "") for tag in tags) for insert in group
        )
        distinct = counts.most_common()
        if len(distinct) > MAX_TABLE_ROWS:
            warnings.append(f"block {name}: {len(distinct)} attribute rows, kept {MAX_TABLE_ROWS}")
            distinct = distinct[:MAX_TABLE_ROWS]
        header = [*tags, "Count"]
        cells = [
            EvidenceCell(row=0, column=column, kind=CellKind.COLUMN_HEADER, text=text)
            for column, text in enumerate(header)
        ]
        for row, (values, count) in enumerate(distinct, start=1):
            cells.extend(
                EvidenceCell(row=row, column=column, text=text)
                for column, text in enumerate((*values, str(count)))
            )
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.TABLE,
                locators=(_sheet(layout),),
                table=EvidenceTable(
                    row_count=len(distinct) + 1,
                    column_count=len(header),
                    cells=tuple(cells),
                    header_rows=1,
                    caption=f"Block {name} attributes ({len(group)} inserts)",
                ),
            )
        )
    return blocks, title_blocks, warnings


def normalize_dwg(
    drawing: DrawingData, *, unit: UnitRecord, plan_id: str, content_hash: str
) -> EvidenceUnit:
    blocks: list[EvidenceBlock] = []
    warnings = list(drawing.warnings)
    title_blocks: list[DrawingInsert] = []
    # (layout, text) that identifiers are read from: content lines and attributes, never frame.
    searchable: list[tuple[str, str]] = []
    budget = MAX_CHARS
    for layout in drawing.layouts:
        texts = [text for text in drawing.texts if text.layout == layout]
        inserts = [insert for insert in drawing.inserts if insert.layout == layout]
        frame = [text for text in texts if _key(text.text) in FRAME_TEXT]
        content = [text for text in texts if _key(text.text) not in FRAME_TEXT]
        attribute_blocks, titles, notes = _attribute_blocks(inserts, layout)
        title_blocks.extend(titles)
        warnings.extend(notes)
        lines = rows(content)
        kept: list[str] = []
        for line in lines:
            if len(line) > budget:
                warnings.append(f"text truncated at {MAX_CHARS} characters")
                break
            kept.append(line)
            budget -= len(line)
        if not (attribute_blocks or kept or frame):
            continue
        heading = "Model space" if layout == "Model" else f"Layout {layout}"
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.HEADING,
                text=heading,
                level=2,
                locators=(_sheet(layout),),
            )
        )
        blocks.extend(attribute_blocks)
        if kept:
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.PARAGRAPH,
                    text="\n".join(kept),
                    locators=(_sheet(layout),),
                )
            )
        if frame:
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.PAGE_FOOTER,
                    text="; ".join(text.text for text in frame),
                    locators=(_sheet(layout),),
                )
            )
        searchable.extend((layout, line) for line in kept)
        searchable.extend((layout, value) for insert in inserts for _, value in insert.attributes)
    if drawing.solids:
        warnings.append(f"{drawing.solids} 3D solids or surfaces: geometry is not read")
    if drawing.xrefs:
        warnings.append("external references not resolved: " + ", ".join(drawing.xrefs))
    content_chars = sum(len(text) for _, text in searchable)
    if not drawing.inserts and content_chars < MIN_CHARS:
        blocks = [block for block in blocks if block.kind is BlockKind.PAGE_FOOTER]
    if any(block.kind is not BlockKind.PAGE_FOOTER for block in blocks):
        blocks.insert(
            0,
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.TITLE,
                text=f"CAD drawing (DWG, {len(drawing.layouts)} layout(s))",
                level=1,
                locators=(Locator(kind=LocatorKind.DOCUMENT),),
            ),
        )
    else:
        blocks = []
    digest = sha256_hex(
        "\n".join(f"{t.layout}\t{t.x:.3f}\t{t.y:.3f}\t{t.text}" for t in drawing.texts)
        + "\n"
        + "\n".join(f"{i.layout}\t{i.block}\t{i.attributes}" for i in drawing.inserts)
    )
    evidence = _unit(
        unit,
        plan_id=plan_id,
        content_hash=content_hash,
        method="native_dwg",
        normalizer=NATIVE_DWG,
        source_digest=digest,
        pages=(),
        blocks=blocks,
        provider={
            "engine": "libredwg+ezdxf",
            "converter": drawing.converter,
            "dxf_version": drawing.dxf_version,
            "codepage": drawing.codepage,
            "layouts": list(drawing.layouts),
            "entities": drawing.entities,
            "solids": drawing.solids,
        },
        warnings=warnings,
    )
    if not blocks:
        return evidence
    return evidence.model_copy(update={"fields": tuple(_fields(title_blocks, searchable))})


def _fields(
    title_blocks: Sequence[DrawingInsert], searchable: Sequence[tuple[str, str]]
) -> Iterator[FieldObservation]:
    for index, insert in enumerate(title_blocks):
        for tag, value in insert.attributes:
            yield FieldObservation(
                path=f"TitleBlock[{index}].{tag}",
                field="TitleBlock",
                method="native",
                evidence_kind=EvidenceKind.EXTRACTED_FIELD,
                value_type="string",
                value=value,
                raw_text=value,
                locators=(_sheet(insert.layout),),
                analyzer_id=NATIVE_DWG,
            )
    found: dict[str, tuple[str, str]] = {}
    for layout, text in searchable:
        for name, pattern in (("DocumentCode", DOCUMENT), ("EquipmentTags", TAG)):
            for match in pattern.findall(text):
                found.setdefault(match.upper(), (name, layout))
    counters: Counter[str] = Counter()
    for value, (name, layout) in found.items():
        yield FieldObservation(
            path=f"{name}[{counters[name]}]",
            field=name,
            method="native",
            evidence_kind=EvidenceKind.EXTRACTED_FIELD,
            value_type="string",
            value=value,
            raw_text=value,
            locators=(_sheet(layout),),
            analyzer_id=NATIVE_DWG,
        )
        counters[name] += 1
