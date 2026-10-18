"""Normalizers (DI Layout, native PDF, spreadsheet, text) and chunkers."""

from __future__ import annotations

from pathlib import Path

import pytest
from evidence_fakes import di_document, di_page, region, simple_layout
from fixture_builders import PdfPage, Sheet, pdf_bytes, xlsx_bytes
from hypothesis import given, settings
from hypothesis import strategies as st

from ingest_pipeline.adapters.parsers.calamine import CalamineSpreadsheetReader
from ingest_pipeline.adapters.parsers.pdfium import PdfiumToolkit
from ingest_pipeline.adapters.parsers.tokenizers import ApproximateCounter
from ingest_pipeline.domain.chunks import ChunkKind, EvidenceChunk
from ingest_pipeline.domain.errors import IntegrityError
from ingest_pipeline.domain.evidence import (
    BlockKind,
    CellKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceFigure,
    EvidenceKind,
    EvidencePage,
    EvidenceTable,
    EvidenceUnit,
    Locator,
    LocatorKind,
    PageStatus,
    a1,
)
from ingest_pipeline.domain.models import ArtifactRef, UnitRecord
from ingest_pipeline.domain.process_cases import process_case_id
from ingest_pipeline.domain.routing import Route
from ingest_pipeline.stages.chunkers import (
    ChunkingSettings,
    DocumentChunker,
    split_text,
    stream_table_bodies,
    table_bodies,
)
from ingest_pipeline.stages.normalizers.di_layout import normalize_layout, resolve_page_map
from ingest_pipeline.stages.normalizers.native import (
    normalize_native_pdf,
    normalize_spreadsheet,
    normalize_text_file,
)

HASH = "c" * 64
RAW = ArtifactRef(sha256="d" * 64, uri="artifacts/raw.json", size_bytes=10, media_type="json")
TOKENS = ApproximateCounter()


def unit(pages=(1,), route=Route.DI_LAYOUT, ordinal=0) -> UnitRecord:
    return UnitRecord(
        unit_id=f"unit:test:{ordinal:05d}", ordinal=ordinal, kind="pdf_pages", route=route.value,
        pages=tuple(pages),
    )


def layout(document, *, pages=(1,), page_map=None, route=Route.DI_LAYOUT) -> EvidenceUnit:
    return normalize_layout(
        document,
        unit=unit(pages, route),
        plan_id="plan:test",
        content_hash=HASH,
        raw=RAW,
        page_map=page_map,
        request={"model_id": "prebuilt-layout"},
    )


def test_page_maps_are_identity_positional_or_explicit() -> None:
    assert resolve_page_map([4, 5], (4, 5), None) == {4: 4, 5: 5}
    assert resolve_page_map([1, 2], (7, 9), None) == {1: 7, 2: 9}
    assert resolve_page_map([1, 2], (7, 9), {1: 7, 2: 9}) == {1: 7, 2: 9}
    with pytest.raises(IntegrityError):
        resolve_page_map([3], (7, 9), None)
    with pytest.raises(IntegrityError):
        resolve_page_map([3], (7, 9), {1: 7})


def test_di_layout_keeps_structure_original_pages_and_spans() -> None:
    evidence = layout(simple_layout([4, 5]), pages=(4, 5))
    assert [page.page for page in evidence.pages] == [4, 5]
    kinds = [block.kind for block in evidence.blocks]
    assert kinds == [
        BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.FIGURE,
        BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.FIGURE,
    ]
    figure = evidence.blocks[2]
    assert figure.figure is not None
    assert figure.figure.figure_id == "4.1"
    assert figure.text == "Figure 4: schema"
    others = [b.text for b in evidence.blocks if b.kind is not BlockKind.FIGURE]
    assert "Figure 4: schema" not in others
    span = next(loc for loc in evidence.blocks[1].locators if loc.kind is LocatorKind.SPAN)
    assert span.index_type == "unicodeCodePoint"
    assert span.artifact_sha256 == RAW.sha256
    locators = evidence.blocks[1].locators
    region_locator = next(loc for loc in locators if loc.kind is LocatorKind.REGION)
    assert region_locator.page == 4
    assert region_locator.unit == "inch"
    assert evidence.provider["api_version"] == "2024-11-30"


def test_di_subset_results_are_mapped_back_to_original_pages() -> None:
    evidence = layout(simple_layout([1, 2]), pages=(30, 31), page_map={1: 30, 2: 31})
    assert [page.page for page in evidence.pages] == [30, 31]
    assert {page for block in evidence.blocks for page in block.pages} == {30, 31}


def test_di_tables_fold_cell_paragraphs_and_detect_headers() -> None:
    paragraphs = [
        {"content": "Titre du manuel", "role": "title", "spans": [{"offset": 0, "length": 15}],
         "boundingRegions": region(1)},
        {"content": "Tag", "spans": [{"offset": 20, "length": 3}], "boundingRegions": region(1)},
        {"content": "Debit", "spans": [{"offset": 25, "length": 5}], "boundingRegions": region(1)},
        {"content": "P-201", "spans": [{"offset": 32, "length": 5}], "boundingRegions": region(2)},
        {"content": "45", "spans": [{"offset": 40, "length": 2}], "boundingRegions": region(2)},
        {"content": "Page 1", "role": "pageNumber", "spans": [{"offset": 50, "length": 6}]},
    ]
    table = {
        "rowCount": 2,
        "columnCount": 2,
        "spans": [{"offset": 20, "length": 22}],
        "boundingRegions": [*region(1), *region(2)],
        "cells": [
            {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Tag",
             "elements": ["/paragraphs/1"], "boundingRegions": region(1)},
            {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "Debit",
             "elements": ["/paragraphs/2"], "boundingRegions": region(1)},
            {"rowIndex": 1, "columnIndex": 0, "content": "P-201", "elements": ["/paragraphs/3"],
             "boundingRegions": region(2)},
            {"rowIndex": 1, "columnIndex": 1, "content": "45", "elements": ["/paragraphs/4"],
             "boundingRegions": region(2)},
        ],
    }
    evidence = layout(
        di_document([di_page(1), di_page(2)], paragraphs=paragraphs, tables=[table]),
        pages=(1, 2),
    )
    assert [block.kind for block in evidence.blocks] == [
        BlockKind.TITLE, BlockKind.TABLE, BlockKind.PAGE_NUMBER,
    ]
    parsed = evidence.blocks[1].table
    assert parsed is not None
    assert parsed.header_rows == 1
    assert parsed.grid() == [["Tag", "Debit"], ["P-201", "45"]]
    assert evidence.blocks[1].pages == (1, 2)


def test_di_pages_without_text_are_marked() -> None:
    evidence = layout(di_document([di_page(1, text=False)]))
    assert evidence.pages[0].status is PageStatus.NO_TEXT
    assert not evidence.has_content


async def test_native_pdf_text_keeps_labels_rotation_and_lines(tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(
        pdf_bytes(
            [
                PdfPage(lines=["Premier paragraphe", "suite"]),
                PdfPage(),
                PdfPage(lines=["Fin"], rotate=90),
            ],
            labels=["i", "ii", "iii"],
        )
    )
    texts = await PdfiumToolkit().page_texts(source, [1, 2, 3])
    evidence = normalize_native_pdf(
        texts, unit=unit((1, 2, 3), Route.NATIVE_PDF_TEXT), plan_id="plan:test", content_hash=HASH
    )
    assert [page.label for page in evidence.pages] == ["i", "ii", "iii"]
    assert evidence.pages[1].status is PageStatus.NO_TEXT
    assert evidence.pages[2].rotation == 90
    first = evidence.blocks[0]
    assert "Premier paragraphe" in first.text
    assert first.pages == (1,)
    lines = next(loc for loc in first.locators if loc.kind is LocatorKind.LINES)
    assert lines.line_start == 1


async def test_spreadsheet_regions_merges_and_cached_values(tmp_path: Path) -> None:
    source = tmp_path / "book.xlsx"
    source.write_bytes(
        xlsx_bytes(
            [
                Sheet(
                    "Feuil1",
                    {
                        "B2": "Equipement", "C2": "Mise en service",
                        "B3": "P-201", "C3": ("date", 45292),
                        "B4": "Total", "C4": ("=1+1", 2),
                        "B8": "Note isolee",
                    },
                    merged=["B4:B5"],
                ),
                Sheet("Masquee", {"A1": "x"}, state="hidden"),
            ]
        )
    )
    sheets = await CalamineSpreadsheetReader().read(source, include_hidden=True, max_cells=1000)
    evidence = normalize_spreadsheet(
        sheets, unit=unit((), Route.NATIVE_SPREADSHEET), plan_id="plan:test", content_hash=HASH
    )
    tables = [block.table for block in evidence.blocks if block.table is not None]
    assert len(tables) == 3
    first = tables[0]
    assert first.header_rows == 1
    assert first.grid()[1] == ["P-201", "2024-01-01"]
    assert first.grid()[2] == ["Total", "2"]
    total = first.cell_at(2, 0)
    assert total is not None
    assert total.row_span == 2
    assert total.locator is not None
    assert total.locator.cell == "B4"
    assert evidence.blocks[1].locators[0].cell == "B8"
    assert tables[2].hidden
    assert any("hidden" in warning for warning in evidence.warnings)


def test_text_files_markdown_and_csv() -> None:
    markdown = normalize_text_file(
        b"# Titre\n\nUn paragraphe\nsur deux lignes.\n\n## Suite\n\nFin.",
        text_format="markdown", unit=unit((), Route.NATIVE_TEXT), plan_id="p", content_hash=HASH,
        max_cells=100,
    )
    assert [(b.kind, b.level) for b in markdown.blocks] == [
        (BlockKind.HEADING, 1), (BlockKind.PARAGRAPH, None), (BlockKind.HEADING, 2),
        (BlockKind.PARAGRAPH, None),
    ]
    assert markdown.blocks[1].text == "Un paragraphe sur deux lignes."
    csv = normalize_text_file(
        "tag;débit\nP-201;45\n".encode("cp1252"),
        text_format="csv", unit=unit((), Route.NATIVE_TEXT), plan_id="p", content_hash=HASH,
        max_cells=100,
    )
    table = csv.blocks[0].table
    assert table is not None
    assert table.header_rows == 1
    assert table.grid() == [["tag", "débit"], ["P-201", "45"]]
    assert csv.provider["encoding"] == "cp1252"


def _prose_unit(texts: list[str], *, route: Route = Route.NATIVE_PDF_TEXT) -> EvidenceUnit:
    blocks = [
        EvidenceBlock(
            evidence_id=f"ev:{index:04d}",
            kind=BlockKind.PARAGRAPH,
            text=text,
            pages=(index // 3 + 1,),
            locators=(Locator(kind=LocatorKind.PAGE, page=index // 3 + 1),),
        )
        for index, text in enumerate(texts)
    ]
    pages = sorted({block.pages[0] for block in blocks})
    return EvidenceUnit(
        unit_id="unit:x:00000", plan_id="plan:x", content_hash=HASH, route=route.value,
        extraction_method="native_text", normalizer="test", blocks=tuple(blocks),
        pages=tuple(EvidencePage(page=page) for page in pages),
    )


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.text(alphabet="abcdefghij éèà.", min_size=1, max_size=900).filter(str.strip),
        min_size=1,
        max_size=25,
    )
)
def test_prose_chunks_lose_no_text_and_bound_overlap(texts: list[str]) -> None:
    settings_ = ChunkingSettings(min_tokens=120, max_tokens=200, overlap_tokens=40)
    chunker = DocumentChunker(evidence_id="evd:x", tokens=TOKENS, settings=settings_)
    unit_ = _prose_unit(texts)
    chunker.feed(unit_)
    chunks = chunker.finish()
    owned = [block_id for chunk in chunks for block_id in chunk.block_ids]
    expected = [block.evidence_id for block in unit_.blocks if block.text.strip()]
    assert list(dict.fromkeys(owned)) == expected
    words = " ".join(" ".join(block.text.split()) for block in unit_.blocks)
    joined = " ".join(" ".join(chunk.text.split()) for chunk in chunks)
    assert all(word in joined for word in words.split())
    for chunk in chunks:
        assert chunk.overlap_tokens <= settings_.overlap_tokens
        assert list(chunk.pages) == sorted(set(chunk.pages))
        assert not set(chunk.context_block_ids) & set(chunk.block_ids) or chunk.context_block_ids
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_headings_set_section_paths_and_furniture_is_excluded() -> None:
    blocks = (
        EvidenceBlock(evidence_id="t", kind=BlockKind.TITLE, text="Manuel broyeur", level=1),
        EvidenceBlock(evidence_id="h1", kind=BlockKind.HEADING, text="Maintenance", level=2),
        EvidenceBlock(evidence_id="p1", kind=BlockKind.PARAGRAPH, text="Graisser les paliers."),
        EvidenceBlock(evidence_id="f", kind=BlockKind.PAGE_FOOTER, text="Confidentiel"),
        EvidenceBlock(evidence_id="h2", kind=BlockKind.HEADING, text="Securite", level=2),
        EvidenceBlock(
            evidence_id="p2", kind=BlockKind.PARAGRAPH, text="Consigner avant intervention."
        ),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.DI_LAYOUT.value,
        extraction_method="di_layout", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(
        evidence_id="evd:y", tokens=TOKENS, settings=ChunkingSettings(min_tokens=600)
    )
    chunker.feed(unit_)
    [chunk] = chunker.finish()
    assert chunk.text.startswith("Manuel broyeur > Maintenance")
    assert "Confidentiel" not in chunk.text
    assert chunk.block_ids == ("p1", "p2")


def test_oversized_generated_descriptions_are_split_to_the_chunk_budget() -> None:
    labels = ", ".join(f"6210-ML-{n:04d}" for n in range(4000))
    blocks = (
        EvidenceBlock(
            evidence_id="fig",
            kind=BlockKind.FIGURE,
            text=f"Visible labels include: {labels}.",
            pages=(7,),
            figure=EvidenceFigure(figure_id="7.1"),
            evidence_kind=EvidenceKind.GENERATED_DESCRIPTION,
        ),
        EvidenceBlock(
            evidence_id="sum",
            kind=BlockKind.PARAGRAPH,
            text="Resume " * 3000,
            evidence_kind=EvidenceKind.GENERATED_DESCRIPTION,
        ),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.CU_ANALYZE.value,
        extraction_method="cu_analyze", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:big", tokens=TOKENS)
    chunker.feed(unit_)
    chunks = chunker.finish()
    figures = [chunk for chunk in chunks if chunk.kind is ChunkKind.FIGURE]
    prose = [chunk for chunk in chunks if chunk.kind is ChunkKind.PROSE]
    assert len(figures) > 1
    assert len(prose) > 1
    assert max(chunk.token_count for chunk in chunks) <= ChunkingSettings().max_tokens
    assert all(chunk.figure_ids == ("7.1",) and chunk.pages == (7,) for chunk in figures)
    joined = " ".join(chunk.text for chunk in figures)
    assert "6210-ML-0000" in joined
    assert "6210-ML-3999" in joined


def test_runs_without_whitespace_are_cut_to_the_budget() -> None:
    parts = split_text("x" * 20_000, 100, TOKENS)
    assert len(parts) > 1
    assert "".join(parts) == "x" * 20_000
    assert max(TOKENS.count(part) for part in parts) <= 100


def test_small_sections_flush_at_headings_when_large_enough() -> None:
    long_text = "mot " * 400
    blocks = (
        EvidenceBlock(evidence_id="h1", kind=BlockKind.HEADING, text="A", level=2),
        EvidenceBlock(evidence_id="p1", kind=BlockKind.PARAGRAPH, text=long_text),
        EvidenceBlock(evidence_id="h2", kind=BlockKind.HEADING, text="B", level=2),
        EvidenceBlock(evidence_id="p2", kind=BlockKind.PARAGRAPH, text="court"),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.DI_LAYOUT.value,
        extraction_method="di_layout", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:z", tokens=TOKENS)
    chunker.feed(unit_)
    chunks = chunker.finish()
    assert [chunk.section_path for chunk in chunks] == [("A",), ("B",)]
    assert chunks[1].context_block_ids == ()


def _span(offset: int, length: int) -> Locator:
    return Locator(
        kind=LocatorKind.SPAN,
        artifact_sha256=HASH,
        representation="di_markdown",
        index_type="unicodeCodePoint",
        offset=offset,
        length=length,
    )


def test_flow_documents_cite_their_text_spans() -> None:
    blocks = (
        EvidenceBlock(
            evidence_id="p1", kind=BlockKind.PARAGRAPH, text="Premier.", locators=(_span(0, 8),)
        ),
        EvidenceBlock(
            evidence_id="p2", kind=BlockKind.PARAGRAPH, text="Second.", locators=(_span(10, 7),)
        ),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.DI_LAYOUT_OFFICE.value,
        extraction_method="di_layout", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:f", tokens=TOKENS)
    chunker.feed(unit_)
    [chunk] = chunker.finish()
    assert chunk.locators == ("span:di_markdown:0+17",)


def test_short_lead_in_joins_the_prose_after_a_table() -> None:
    table = _table(3, 2)
    blocks = (
        EvidenceBlock(evidence_id="lead", kind=BlockKind.PARAGRAPH, text="Review Sheet Document"),
        EvidenceBlock(evidence_id="t", kind=BlockKind.TABLE, text="", table=table),
        EvidenceBlock(evidence_id="body", kind=BlockKind.PARAGRAPH, text="mot " * 120),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.DI_LAYOUT_OFFICE.value,
        extraction_method="di_layout", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:s", tokens=TOKENS)
    chunker.feed(unit_)
    chunks = chunker.finish()
    prose = [chunk for chunk in chunks if chunk.kind is ChunkKind.PROSE]
    assert [chunk.block_ids for chunk in prose] == [("lead", "body")]
    assert any(chunk.kind is ChunkKind.TABLE for chunk in chunks)


def _table(rows: int, columns: int, *, width: int = 4) -> EvidenceTable:
    cells = [
        EvidenceCell(row=0, column=c, kind=CellKind.COLUMN_HEADER, text=f"col{c}")
        for c in range(columns)
    ]
    cells += [
        EvidenceCell(row=r, column=c, text=f"r{r}c{c}" + "x" * width)
        for r in range(1, rows)
        for c in range(columns)
    ]
    return EvidenceTable(row_count=rows, column_count=columns, cells=tuple(cells), header_rows=1)


def test_table_chunks_repeat_headers_and_never_split_rows() -> None:
    bodies = list(table_bodies(_table(60, 3), 120, TOKENS))
    assert len(bodies) > 1
    seen: list[int] = []
    for body, (first, last), columns in bodies:
        assert body.splitlines()[0] == "| col0 | col1 | col2 |"
        assert columns == (0, 2)
        seen.extend(range(first, last + 1))
        for row in range(first, last + 1):
            assert f"| r{row}c0" in body
            assert f"r{row}c2" in body
    assert seen == list(range(1, 60))


def test_later_row_groups_cite_their_own_rows_and_the_header_apart() -> None:
    base = _table(60, 3)
    cells = tuple(
        cell.model_copy(
            update={
                "locator": Locator(
                    kind=LocatorKind.CELL,
                    sheet="Feuil1",
                    cell=a1(cell.row + 2, cell.column + 1),
                    row=cell.row + 2,
                    column=cell.column + 1,
                )
            }
        )
        for cell in base.cells
    )
    table = base.model_copy(update={"cells": cells, "sheet": "Feuil1"})
    block = EvidenceBlock(evidence_id="t", kind=BlockKind.TABLE, text="", table=table)
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.NATIVE_SPREADSHEET.value,
        extraction_method="native_spreadsheet", normalizer="test", blocks=(block,),
    )
    chunker = DocumentChunker(
        evidence_id="evd:t", tokens=TOKENS, settings=ChunkingSettings(table_max_tokens=120)
    )
    chunker.feed(unit_)
    chunks = chunker.finish()

    assert len(chunks) > 1

    def rows(locator: str) -> tuple[int, int]:
        start, end = locator.split("!")[1].split(":")
        return int(start[1:]), int(end[1:])

    [first] = chunks[0].locators
    header, body = chunks[1].locators
    assert rows(first)[0] == 2
    assert header == "cell:Feuil1!A2:C2"
    assert rows(body)[0] == rows(first)[1] + 1


def test_wide_rows_split_by_column_groups_with_the_row_key() -> None:
    bodies = list(table_bodies(_table(3, 12, width=30), 120, TOKENS))
    groups = sorted({columns for _, _, columns in bodies})
    assert len(groups) > 1
    assert groups[0][0] == 0
    assert groups[-1][1] == 11
    for body, (first_row, last_row), (first, _) in bodies:
        if first > 0:
            assert "| col0 |" in body.splitlines()[0]
        for row in range(first_row, last_row + 1):
            assert f"| r{row}c0" in body


def test_drawing_units_chunk_per_sheet() -> None:
    blocks = (
        EvidenceBlock(evidence_id="a", kind=BlockKind.PARAGRAPH, text="BR-101 broyeur", pages=(4,)),
        EvidenceBlock(evidence_id="b", kind=BlockKind.PARAGRAPH, text="P-201 pompe", pages=(5,)),
    )
    unit_ = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.DI_LAYOUT_DRAWING.value,
        extraction_method="di_layout", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:d", tokens=TOKENS)
    chunker.feed(unit_)
    chunks = chunker.finish()
    assert [(chunk.kind, chunk.pages) for chunk in chunks] == [
        (ChunkKind.DRAWING_TEXT, (4,)),
        (ChunkKind.DRAWING_TEXT, (5,)),
    ]


STREAM_ROWS = (
    ("Stream No", "", "352", "", "404", "409", "409", "433"),
    ("Description", "Units", "Sample Return", "", "Dewatering", "Tails", "Tails", "Floc"),
    ("Dry Solids Flowrate", "t/h", "4,20", "", "116", "146", "435", ""),
    ("Water/Solution Flowrate", "m3/h", "10,4", "", "547", "366", "1082", "52,2"),
    ("%Solids by weight", "m/m%", "28,7", "%", "17,5%", "28,5%", "28,7%", ""),
)
LABELS = ("Stream No", "Description", "Dry Solids Flowrate", "Water/Solution", "Comments")


def _stream_table(notes: tuple[EvidenceCell, ...] | None = None) -> EvidenceTable:
    """A PFD stream table: printed cases sit in the comments row under repeated stream 409."""

    cells = [
        EvidenceCell(row=r, column=c, text=text)
        for r, row in enumerate(STREAM_ROWS)
        for c, text in enumerate(row)
        if text
    ]
    cells.append(EvidenceCell(row=5, column=0, column_span=2, text="Comments"))
    cells += (
        notes
        if notes is not None
        else (
            EvidenceCell(row=5, column=5, text="With Backfill"),
            EvidenceCell(row=5, column=6, text="Without\nBackfill"),
        )
    )
    return EvidenceTable(row_count=6, column_count=8, cells=tuple(cells), header_rows=1)


def _drawing_chunks(table: EvidenceTable, **settings: int) -> list[EvidenceChunk]:
    block = EvidenceBlock(evidence_id="t", kind=BlockKind.TABLE, text="", pages=(22,), table=table)
    unit_ = EvidenceUnit(
        unit_id="u",
        plan_id="p",
        content_hash=HASH,
        route=Route.DI_LAYOUT_DRAWING.value,
        extraction_method="di_layout",
        normalizer="test",
        blocks=(block,),
    )
    chunker = DocumentChunker(
        evidence_id="evd:s", tokens=TOKENS, settings=ChunkingSettings(**settings)
    )
    chunker.feed(unit_)
    return chunker.finish()


def test_stream_tables_split_by_case_and_keep_stream_units_and_comments() -> None:
    chunks = _drawing_chunks(_stream_table())

    assert [chunk.kind for chunk in chunks] == [ChunkKind.DRAWING_TABLE] * 4
    for chunk in chunks:
        assert all(label in chunk.text for label in LABELS)
        assert "| m3/h |" in chunk.text
    with_case, without_case = process_case_id("with-backfill"), process_case_id("without-backfill")
    assert [chunk.process_case_id for chunk in chunks] == [None, with_case, without_case, None]
    assert "| 146 |" in chunks[1].text
    assert "435" not in chunks[1].text
    assert "| 435 |" in chunks[2].text
    assert "Without Backfill" in chunks[2].text
    assert "| 28,7 | % | 17,5% |" in chunks[0].text
    assert chunks[1].locators == ("page:22", "table:rows1-6", "table:cols6-6")


def test_stream_tables_split_by_columns_never_by_rows() -> None:
    table = _stream_table(notes=())
    bodies = stream_table_bodies(table, 70, TOKENS)

    assert bodies is not None
    assert len(bodies) > 1
    assert bodies[0][2][0] == 2
    for body, rows, _, case in bodies:
        assert rows == (0, 5)
        assert case is None
        assert all(label in body for label in LABELS)


def test_a_note_merged_across_streams_binds_no_case() -> None:
    merged = (EvidenceCell(row=5, column=5, column_span=2, text="With Backfill"),)
    chunks = _drawing_chunks(_stream_table(notes=merged))

    assert {chunk.process_case_id for chunk in chunks} == {None}


def test_stream_tables_fall_back_to_row_groups_when_a_column_cannot_fit() -> None:
    assert stream_table_bodies(_stream_table(), 20, TOKENS) is None
    assert stream_table_bodies(_table(3, 3), 900, TOKENS) is None

    table = _stream_table()
    chunks = _drawing_chunks(table, table_max_tokens=60)

    assert {chunk.process_case_id for chunk in chunks} == {None}
    assert [chunk.text.split("\n\n", 1)[1] for chunk in chunks] == [
        body for body, _, _ in table_bodies(table, 60, TOKENS)
    ]


def test_chunk_identity_depends_on_tokenizer() -> None:
    class Other(ApproximateCounter):
        @property
        def name(self) -> str:
            return "other"

    unit_ = _prose_unit(["Graisser les paliers du broyeur."])
    first = DocumentChunker(evidence_id="evd:x", tokens=TOKENS)
    second = DocumentChunker(evidence_id="evd:x", tokens=Other())
    first.feed(unit_)
    second.feed(unit_)
    [a], [b] = first.finish(), second.finish()
    assert a.text == b.text
    assert a.chunk_id != b.chunk_id
