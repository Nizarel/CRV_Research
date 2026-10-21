"""Structure-aware chunkers over canonical evidence (prose, table rows, drawing sheets, figures).

Chunks never cross a drawing sheet, never mix prose with tables, carry a content-derived
title/section header, and keep page furniture out of their text. Overlap is bounded and only
inside one section; overlapped blocks are recorded as context, not as new evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..domain.chunks import ChunkKind, EvidenceChunk, chunk_id_for
from ..domain.evidence import (
    FURNITURE,
    BlockKind,
    EvidenceBlock,
    EvidenceKind,
    EvidenceTable,
    EvidenceUnit,
    Locator,
    LocatorKind,
    a1,
)
from ..domain.furniture import REMOVABLE, furniture_key
from ..domain.process_cases import case_key, process_case_id
from ..domain.routing import Route, UnitKind
from ..ports.parsing import TokenCounter

CHUNKER = "evidence-chunker@7"
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")
_SPAN = re.compile(r"^span:(?P<rep>[^:]+):(?P<offset>\d+)\+(?P<length>\d+)$")
_STREAM_ANCHOR = re.compile(r"^stream\s*(?:no\b|n°|number)", re.IGNORECASE)
_UNITS_LABEL = re.compile(r"^units?$", re.IGNORECASE)
_COMMENTS_LABEL = re.compile(r"^comments?\b", re.IGNORECASE)


@dataclass(frozen=True)
class ChunkingSettings:
    min_tokens: int = 600
    max_tokens: int = 900
    overlap_tokens: int = 100
    table_max_tokens: int = 900
    sliver_tokens: int = 48
    allow_partial_projection: bool = False
    scientific_observations: bool = False

    def identity(self) -> dict[str, int]:
        return {
            "min_tokens": self.min_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "table_max_tokens": self.table_max_tokens,
            "sliver_tokens": self.sliver_tokens,
            "scientific_observations": int(self.scientific_observations),
        }


@dataclass
class _Piece:
    block_id: str
    text: str
    tokens: int
    pages: tuple[int, ...]
    locators: tuple[str, ...]
    unit_id: str


def _locators(block: EvidenceBlock) -> tuple[str, ...]:
    kept = [loc for loc in block.locators if loc.kind is not LocatorKind.SPAN]
    # Flow documents (DOCX, HTML) have no geometry, so their text spans are the citation.
    chosen = kept or list(block.locators)
    return tuple(dict.fromkeys(locator.compact() for locator in chosen))


def _coalesce_spans(locators: Iterable[str]) -> tuple[str, ...]:
    """Collapse the span locators of each representation into one covering range."""

    ranges: dict[str, tuple[int, int]] = {}
    others: list[str] = []
    for locator in locators:
        match = _SPAN.match(locator)
        if match is None:
            others.append(locator)
            continue
        start = int(match["offset"])
        end = start + int(match["length"])
        low, high = ranges.get(match["rep"], (start, end))
        ranges[match["rep"]] = (min(low, start), max(high, end))
    spans = [f"span:{rep}:{low}+{high - low}" for rep, (low, high) in ranges.items()]
    return tuple(dict.fromkeys([*others, *spans]))


def _fit(unit: str, budget: int, tokens: TokenCounter) -> list[str]:
    """``unit`` as is, or cut into character runs of at most ``budget`` tokens each."""

    parts: list[str] = []
    while unit:
        size = min(len(unit), budget * 4)
        while size > 1 and tokens.count(unit[:size]) > budget:
            size //= 2
        parts.append(unit[:size])
        unit = unit[size:]
    return parts


def split_text(text: str, budget: int, tokens: TokenCounter) -> list[str]:
    """Greedy split on sentences, then words, then characters, so every part fits ``budget``."""

    if tokens.count(text) <= budget:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    for sentence in _SENTENCE.split(text):
        units = (
            [sentence]
            if tokens.count(sentence) <= budget
            else [piece for word in sentence.split() for piece in _fit(word, budget, tokens)]
        )
        for unit in units:
            candidate = " ".join([*current, unit])
            if current and tokens.count(candidate) > budget:
                parts.append(" ".join(current))
                current = [unit]
            else:
                current.append(unit)
    if current:
        parts.append(" ".join(current))
    return [part for part in parts if part.strip()]


@dataclass
class DocumentChunker:
    """Feed units in plan order, then ``finish``; chunk ordinals follow source order."""

    evidence_id: str
    tokens: TokenCounter
    settings: ChunkingSettings = field(default_factory=ChunkingSettings)
    chunks: list[EvidenceChunk] = field(default_factory=list)
    title: str | None = None
    _sections: list[tuple[int, str]] = field(default_factory=list)
    _buffer: list[_Piece] = field(default_factory=list)
    _context: list[_Piece] = field(default_factory=list)
    _path: tuple[str, ...] = ()
    _language: str | None = None
    running: frozenset[str] = frozenset()
    """Furniture keys of lines repeated across the document's pages (``FurnitureScan``)."""

    @property
    def section_path(self) -> tuple[str, ...]:
        return tuple(text for _, text in self._sections)

    def _header(self, extra: str | None = None, path: tuple[str, ...] | None = None) -> str:
        sections = self.section_path if path is None else path
        parts = [part for part in (self.title, *sections, extra) if part]
        return " > ".join(dict.fromkeys(parts))

    def _emit(
        self,
        kind: ChunkKind,
        body: str,
        *,
        block_ids: tuple[str, ...],
        pages: tuple[int, ...],
        locators: tuple[str, ...],
        unit_ids: tuple[str, ...],
        header: str | None = None,
        context_ids: tuple[str, ...] = (),
        overlap: int = 0,
        figure_ids: tuple[str, ...] = (),
        sheet: str | None = None,
        path: tuple[str, ...] | None = None,
        evidence_kind: EvidenceKind = EvidenceKind.SOURCE_TEXT,
        case: str | None = None,
    ) -> None:
        heading = self._header(path=path) if header is None else header
        text = f"{heading}\n\n{body}" if heading else body
        if not body.strip():
            return
        self.chunks.append(
            EvidenceChunk(
                chunk_id=chunk_id_for(
                    evidence_id=self.evidence_id,
                    chunker=CHUNKER,
                    tokenizer=self.tokens.name,
                    block_ids=(*context_ids, *block_ids),
                    text=text,
                ),
                evidence_id=self.evidence_id,
                ordinal=len(self.chunks),
                kind=kind,
                text=text,
                token_count=max(self.tokens.count(text), 1),
                tokenizer=self.tokens.name,
                chunker=CHUNKER,
                block_ids=tuple(dict.fromkeys(block_ids)),
                context_block_ids=tuple(dict.fromkeys(context_ids)),
                pages=tuple(sorted(set(pages))),
                locators=_coalesce_spans(locators),
                section_path=self.section_path if path is None else path,
                unit_ids=tuple(dict.fromkeys(unit_ids)),
                figure_ids=figure_ids,
                sheet=sheet,
                language=self._language,
                overlap_tokens=overlap,
                evidence_kind=evidence_kind,
                process_case_id=process_case_id(case) if case else None,
            )
        )

    def _flush(self, *, carry: bool) -> None:
        if not self._buffer:
            self._context = []
            return
        context = self._context
        pieces = [*context, *self._buffer]
        self._emit(
            ChunkKind.PROSE,
            "\n\n".join(piece.text for piece in pieces),
            block_ids=tuple(piece.block_id for piece in self._buffer),
            context_ids=tuple(piece.block_id for piece in context),
            overlap=sum(piece.tokens for piece in context),
            pages=tuple(page for piece in pieces for page in piece.pages),
            locators=tuple(loc for piece in self._buffer for loc in piece.locators),
            unit_ids=tuple(piece.unit_id for piece in self._buffer),
            path=self._path,
        )
        self._context = []
        if carry and self.settings.overlap_tokens:
            budget = self.settings.overlap_tokens
            for piece in reversed(self._buffer):
                if piece.tokens > budget:
                    break
                self._context.insert(0, piece)
                budget -= piece.tokens
        self._buffer = []

    def _break(self) -> None:
        # A short lead-in (a title line or label) joins the prose after the table instead.
        if sum(piece.tokens for piece in self._buffer) >= self.settings.sliver_tokens:
            self._flush(carry=False)

    def _budget(self) -> int:
        path = self._path if self._buffer else self.section_path
        return max(self.settings.max_tokens - self.tokens.count(self._header(path=path)) - 4, 32)

    def _add_prose(self, piece: _Piece) -> None:
        budget = self._budget()
        used = sum(p.tokens for p in (*self._context, *self._buffer))
        if self._buffer and used + piece.tokens > budget:
            self._flush(carry=True)
            used = sum(p.tokens for p in self._context)
            if used + piece.tokens > budget:
                self._context = []
        if not self._buffer:
            self._path = self.section_path
        self._buffer.append(piece)

    def _heading(self, block: EvidenceBlock) -> None:
        level = block.level or 1
        if block.kind is BlockKind.TITLE and self.title is None:
            self.title = block.text
            return
        used = sum(piece.tokens for piece in self._buffer)
        if used >= self.settings.min_tokens // 2:
            self._flush(carry=False)
        elif not self._buffer:
            self._context = []
        while self._sections and self._sections[-1][0] >= level:
            self._sections.pop()
        self._sections.append((level, block.text))

    def _prose(self, block: EvidenceBlock, unit: EvidenceUnit) -> None:
        budget = self._budget()
        for part in split_text(block.text, budget, self.tokens):
            self._add_prose(
                _Piece(
                    block_id=block.evidence_id,
                    text=part,
                    tokens=self.tokens.count(part),
                    pages=block.pages,
                    locators=_locators(block),
                    unit_id=unit.unit_id,
                )
            )

    def _table(
        self,
        block: EvidenceBlock,
        table: EvidenceTable,
        unit: EvidenceUnit,
        *,
        kind: ChunkKind,
        header: str,
    ) -> None:
        budget = self.settings.table_max_tokens
        streams = stream_table_bodies(table, budget, self.tokens)
        if streams is not None:
            for body, rows, columns, case in streams:
                self._emit(
                    kind,
                    body,
                    block_ids=(block.evidence_id,),
                    pages=block.pages,
                    locators=(*_table_locators(block, table, rows, columns),),
                    unit_ids=(unit.unit_id,),
                    header=header,
                    sheet=table.sheet,
                    case=case,
                )
            return
        for body, rows, columns in table_bodies(table, budget, self.tokens):
            self._emit(
                kind,
                body,
                block_ids=(block.evidence_id,),
                pages=block.pages,
                locators=(*_table_locators(block, table, rows, columns),),
                unit_ids=(unit.unit_id,),
                header=header,
                sheet=table.sheet,
            )

    def _figure(self, block: EvidenceBlock, unit: EvidenceUnit, header: str | None = None) -> None:
        figure = block.figure
        if figure is None or not block.text.strip():
            return
        # Generated drawing descriptions can list every label on a sheet (60k+ tokens).
        for part in split_text(block.text, self._standalone_budget(header), self.tokens):
            self._emit(
                ChunkKind.FIGURE,
                part,
                block_ids=(block.evidence_id,),
                pages=block.pages,
                locators=_locators(block),
                unit_ids=(unit.unit_id,),
                figure_ids=(figure.figure_id,),
                header=header,
                evidence_kind=block.evidence_kind,
            )

    def _standalone_budget(self, header: str | None) -> int:
        heading = self._header() if header is None else header
        return max(self.settings.max_tokens - self.tokens.count(heading) - 4, 32)

    def _generated(
        self, block: EvidenceBlock, unit: EvidenceUnit, header: str | None = None
    ) -> None:
        """Model-generated text stands alone: never merged with source text it could mask."""

        if block.figure is not None:
            self._figure(block, unit, header)
            return
        if not block.text.strip():
            return
        for part in split_text(block.text, self._standalone_budget(header), self.tokens):
            self._emit(
                ChunkKind.PROSE,
                part,
                block_ids=(block.evidence_id,),
                pages=block.pages,
                locators=_locators(block),
                unit_ids=(unit.unit_id,),
                header=header,
                evidence_kind=block.evidence_kind,
            )

    def _is_running(self, block: EvidenceBlock) -> bool:
        return block.kind in REMOVABLE and furniture_key(block.text) in self.running

    def feed(self, unit: EvidenceUnit) -> None:
        self._language = unit.language or self._language
        if _is_drawing(unit):
            self._flush(carry=False)
            self._drawing(unit)
            return
        for block in unit.blocks:
            if block.kind in FURNITURE or self._is_running(block):
                continue
            if block.evidence_kind is not EvidenceKind.SOURCE_TEXT:
                self._break()
                self._generated(block, unit)
            elif block.kind in (BlockKind.TITLE, BlockKind.HEADING):
                self._heading(block)
            elif block.kind is BlockKind.TABLE and block.table is not None:
                self._break()
                header = self._header(block.table.caption or block.table.sheet)
                self._table(block, block.table, unit, kind=ChunkKind.TABLE, header=header)
            elif block.kind is BlockKind.FIGURE:
                self._break()
                self._figure(block, unit)
            elif block.text.strip():
                self._prose(block, unit)

    def _drawing(self, unit: EvidenceUnit) -> None:
        """One drawing sheet per page; sheets are never merged and keep the title block text."""

        sheets: dict[int | None, list[EvidenceBlock]] = {}
        for block in unit.blocks:
            if block.kind not in FURNITURE:
                sheets.setdefault(min(block.pages) if block.pages else None, []).append(block)
        for page, blocks in sorted(sheets.items(), key=lambda item: item[0] or 0):
            header = self._header(f"sheet p{page}" if page else None)
            texts: list[EvidenceBlock] = []
            for block in blocks:
                if block.evidence_kind is not EvidenceKind.SOURCE_TEXT:
                    self._generated(block, unit, header)
                elif block.kind is BlockKind.TABLE and block.table is not None:
                    self._table(
                        block, block.table, unit, kind=ChunkKind.DRAWING_TABLE, header=header
                    )
                elif block.kind is BlockKind.FIGURE:
                    self._figure(block, unit, header)
                elif block.text.strip():
                    texts.append(block)
            budget = max(self.settings.max_tokens - self.tokens.count(header) - 4, 32)
            for group in _pack(texts, budget, self.tokens):
                self._emit(
                    ChunkKind.DRAWING_TEXT,
                    "\n".join(block.text for block in group),
                    block_ids=tuple(block.evidence_id for block in group),
                    pages=(page,) if page else (),
                    locators=tuple(loc for block in group for loc in _locators(block)),
                    unit_ids=(unit.unit_id,),
                    header=header,
                )

    def finish(self) -> list[EvidenceChunk]:
        self._flush(carry=False)
        return self.chunks


def _is_drawing(unit: EvidenceUnit) -> bool:
    return (
        unit.route == Route.DI_LAYOUT_DRAWING.value
        or unit.provider.get("unit_kind") == UnitKind.DRAWING_SHEET.value
    )


def _pack(
    blocks: list[EvidenceBlock], budget: int, tokens: TokenCounter
) -> Iterator[list[EvidenceBlock]]:
    group: list[EvidenceBlock] = []
    used = 0
    for block in blocks:
        size = tokens.count(block.text)
        if group and used + size > budget:
            yield group
            group, used = [], 0
        group.append(block)
        used += size
    if group:
        yield group


def _row_text(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"


def table_bodies(
    table: EvidenceTable, budget: int, tokens: TokenCounter
) -> Iterator[tuple[str, tuple[int, int], tuple[int, int]]]:
    """Markdown bodies of row groups (header rows repeated); wide rows split by column group.

    Yields ``(body, (first_row, last_row), (first_column, last_column))`` with 0-based indices.
    """

    grid = table.grid()
    if not grid:
        return
    width = table.column_count
    header_rows = grid[: table.header_rows]
    indexed = [
        (index + table.header_rows, row)
        for index, row in enumerate(grid[table.header_rows :])
        if any(cell.strip() for cell in row)
    ]
    if not indexed:
        if header_rows:
            body = "\n".join(_row_text(row) for row in header_rows)
            yield body, (0, len(header_rows) - 1), (0, width - 1)
        return
    lead = [*([table.caption] if table.caption else []), *table.footnotes]
    for columns in _column_groups(header_rows, [row for _, row in indexed], budget, tokens):
        first_col, last_col = columns
        keep = list(range(first_col, last_col + 1))
        if first_col > 0:
            keep = [0, *keep]
        head = [_row_text([row[c] for c in keep]) for row in header_rows]
        if head:
            head.append("| " + " | ".join("---" for _ in keep) + " |")
        head_text = "\n".join([*lead, *head])
        head_tokens = tokens.count(head_text) if head_text else 0
        group: list[tuple[int, str]] = []
        used = head_tokens
        for index, row in indexed:
            line = _row_text([row[c] for c in keep])
            size = tokens.count(line)
            if group and used + size > budget:
                yield _body(head_text, group), (group[0][0], group[-1][0]), columns
                group, used = [], head_tokens
            group.append((index, line))
            used += size
        if group:
            yield _body(head_text, group), (group[0][0], group[-1][0]), columns


def _body(head: str, group: list[tuple[int, str]]) -> str:
    lines = "\n".join(line for _, line in group)
    return f"{head}\n{lines}" if head else lines


def _note_case(table: EvidenceTable, row: int, column: int, labelled: set[int]) -> str | None:
    """Case printed in this column's note; a note merged across streams binds to none."""

    cell = table.cell_at(row, column)
    if cell is None or set(range(cell.column, cell.column + cell.column_span)) & (
        labelled - {column}
    ):
        return None
    return case_key(cell.text)


def stream_table_bodies(
    table: EvidenceTable, budget: int, tokens: TokenCounter
) -> list[tuple[str, tuple[int, int], tuple[int, int], str | None]] | None:
    """Split a process stream table by value columns, never by rows.

    Every body keeps the stream-number row, the parameter and unit columns and the comments
    row, so a value is never cut off from its stream, unit or note. Column groups never mix
    printed cases. Returns ``None`` when the table has no ``Stream No`` row or one column
    does not fit ``budget``; items are ``(body, rows, columns, case_key)`` as in ``table_bodies``.
    """

    grid = table.grid()
    anchor = next(
        (
            (row, column)
            for row, cells in enumerate(grid)
            for column, text in enumerate(cells)
            if _STREAM_ANCHOR.match(text.strip())
        ),
        None,
    )
    if anchor is None:
        return None
    top, stub = anchor
    width = table.column_count
    below = range(top + 1, table.row_count)
    lead_end = stub
    if stub + 1 < width and any(_UNITS_LABEL.match(grid[r][stub + 1].strip()) for r in below):
        lead_end = stub + 1
    lead = list(range(lead_end + 1))
    values = [c for c in range(len(lead), width) if any(cells[c].strip() for cells in grid)]
    if not values:
        return None
    labelled = {column for column in values if grid[top][column].strip()}
    comments = next((r for r in below if _COMMENTS_LABEL.match(grid[r][stub].strip())), None)
    rows = [r for r in range(table.row_count) if any(grid[r][c].strip() for c in lead + values)]
    # Unlabelled columns are split cells of the stream before them (e.g. a lone "%").
    streams: list[list[int]] = []
    for column in values:
        if column in labelled or not streams:
            streams.append([column])
        else:
            streams[-1].append(column)
    cases: list[str | None] = []
    for columns in streams:
        found = (
            {_note_case(table, comments, c, labelled) for c in columns} - {None}
            if comments is not None
            else set()
        )
        cases.append(found.pop() if len(found) == 1 else None)
    lead_line = [table.caption] if table.caption else []

    def render(columns: list[int]) -> str:
        kept = lead + columns
        lines: list[tuple[int, str]] = []
        for r in rows:
            line = _row_text([grid[r][c] for c in kept])
            # Row-spanned cells repeat in the grid; one copy of an identical row is enough.
            if not lines or lines[-1][1] != line:
                lines.append((r, line))
        rule = "| " + " | ".join("---" for _ in kept) + " |"
        head = [line for r, line in lines if r <= top]
        body = [line for r, line in lines if r > top]
        return "\n".join([*lead_line, *head, rule, *body])

    pieces: list[tuple[list[int], str | None]] = []
    for columns, case in zip(streams, cases, strict=True):
        if len(columns) > 1 and tokens.count(render(columns)) > budget:
            pieces.extend(([column], case) for column in columns)
        else:
            pieces.append((columns, case))
    bodies: list[tuple[str, tuple[int, int], tuple[int, int], str | None]] = []
    group: list[int] = []
    group_case: str | None = None
    for columns, case in pieces:
        if group and case == group_case and tokens.count(render(group + columns)) <= budget:
            group += columns
            continue
        if group:
            bodies.append((render(group), (rows[0], rows[-1]), (group[0], group[-1]), group_case))
        if tokens.count(render(columns)) > budget:
            return None
        group, group_case = list(columns), case
    bodies.append((render(group), (rows[0], rows[-1]), (group[0], group[-1]), group_case))
    return bodies


def _column_groups(
    header_rows: list[list[str]], rows: list[list[str]], budget: int, tokens: TokenCounter
) -> list[tuple[int, int]]:
    """Contiguous column ranges such that the widest row of each range fits the budget."""

    if not rows:
        return []
    width = len(rows[0])
    sample = [*header_rows, *rows]
    widest = max(sample, key=lambda row: len(_row_text(row)))
    if tokens.count(_row_text(widest)) * 2 <= budget or width <= 1:
        return [(0, width - 1)]
    groups: list[tuple[int, int]] = []
    start = 0
    for column in range(1, width + 1):
        if column == width:
            groups.append((start, column - 1))
            break
        keep = [0, *range(max(start, 1), column + 1)] if start else list(range(0, column + 1))
        if tokens.count(_row_text([widest[c] for c in keep])) * 2 > budget and column > start:
            groups.append((start, column - 1))
            start = column
    return groups


def _table_locators(
    block: EvidenceBlock,
    table: EvidenceTable,
    rows: tuple[int, int],
    columns: tuple[int, int],
) -> list[str]:
    found = [
        cell.locator
        for cell in table.cells
        if cell.locator is not None
        and (cell.row < table.header_rows or rows[0] <= cell.row <= rows[1])
        and columns[0] <= cell.column <= columns[1]
    ]
    cells = [loc for loc in found if loc.kind is LocatorKind.CELL and loc.row and loc.column]
    if cells:
        sheet = f"{cells[0].sheet}!" if cells[0].sheet else ""
        head = [
            cell.locator
            for cell in table.cells
            if cell.row < table.header_rows and cell.locator in cells
        ]
        body = [loc for loc in cells if loc not in head]
        # Cite repeated header rows apart from the body unless they are contiguous,
        # so a later row group is never cited as covering the rows before it.
        if head and body and _max_row(head) + 1 < _min_row(body):
            return [f"cell:{sheet}{_a1_span(head)}", f"cell:{sheet}{_a1_span(body)}"]
        return [f"cell:{sheet}{_a1_span(cells)}"]
    pages = sorted({loc.page for loc in found if loc.page}) or list(block.pages)
    markers = [f"table:rows{rows[0] + 1}-{rows[1] + 1}"]
    if columns != (0, table.column_count - 1):
        markers.append(f"table:cols{columns[0] + 1}-{columns[1] + 1}")
    return [*(f"page:{page}" for page in pages), *markers]


def _min_row(cells: list[Locator]) -> int:
    return min(loc.row or 1 for loc in cells)


def _max_row(cells: list[Locator]) -> int:
    return max(loc.row or 1 for loc in cells)


def _a1_span(cells: list[Locator]) -> str:
    first = a1(_min_row(cells), min(loc.column or 1 for loc in cells))
    last = a1(_max_row(cells), max(loc.column or 1 for loc in cells))
    return first if first == last else f"{first}:{last}"
