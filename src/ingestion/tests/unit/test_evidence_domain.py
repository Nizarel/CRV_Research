"""Routing, sniffing and evidence model rules."""

from __future__ import annotations

import pytest

from ingest_pipeline.domain.errors import UnsupportedContentError
from ingest_pipeline.domain.evidence import (
    BlockKind,
    CellKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceTable,
    Locator,
    LocatorKind,
    a1,
    detect_language,
    normalize_text,
)
from ingest_pipeline.domain.models import UnitDisposition
from ingest_pipeline.domain.routing import (
    Family,
    PageSignals,
    PreflightReport,
    Route,
    RoutingPolicy,
    SubmitMode,
    UnitKind,
    classify_page,
    plan_id_for,
    plan_units,
)
from ingest_pipeline.domain.sniff import FileKind, ZipLimits, check_zip_safety, sniff
from ingest_pipeline.stages.common import page_ranges

HASH = "a" * 64
OOXML = ["[Content_Types].xml", "_rels/.rels"]


def page(number: int, *, chars: int = 500, images: int = 0, paths: int = 0, size=(595, 842)):
    return PageSignals(
        page=number,
        width_pt=size[0],
        height_pt=size[1],
        text_chars=chars,
        image_objects=images,
        path_objects=paths,
    )


def report(
    pages=(), *, family=Family.PDF, kind=FileKind.PDF, size=1000, **fields
) -> PreflightReport:
    return PreflightReport(
        content_hash=HASH,
        size_bytes=size,
        snapshot_id="snap-1",
        blob_name="Broyage/doc.pdf",
        extension=".pdf",
        file_kind=kind,
        family=family,
        page_count=len(pages) or None,
        pages=tuple(pages),
        **fields,
    )


@pytest.mark.parametrize(
    ("head", "name", "entries", "kind"),
    [
        (b"%PDF-1.7\n", "x.pdf", None, FileKind.PDF),
        (b"PK\x03\x04", "x.xlsx", [*OOXML, "xl/workbook.xml"], FileKind.XLSX),
        (b"PK\x03\x04", "x.xlsb", [*OOXML, "xl/workbook.bin"], FileKind.XLSB),
        (b"PK\x03\x04", "x.docx", [*OOXML, "word/document.xml"], FileKind.DOCX),
        (b"PK\x03\x04", "x.pptx", [*OOXML, "ppt/presentation.xml"], FileKind.PPTX),
        (b"PK\x03\x04", "x.zip", ["a.txt"], FileKind.ZIP),
        (b"\x89PNG\r\n\x1a\n....", "x.png", None, FileKind.PNG),
        (b"MZ\x90\x00", "setup.exe", None, FileKind.EXECUTABLE),
        (b"", "vide.txt", None, FileKind.EMPTY),
        (b"tag;description\nP-201;pompe\n", "x.csv", None, FileKind.TEXT),
        (b"<!DOCTYPE html><html>", "x.htm", None, FileKind.HTML),
        (b"\x00\x01\x02binary", "x.bin", None, FileKind.UNKNOWN),
        (b"AC1027\x00\x00\x00\x00\x00\x06", "plan.dwg", None, FileKind.DWG),
        (b"  0\r\nSECTION\r\n  2\r\nHEADER\r\n", "part.dxf", None, FileKind.DXF),
        (b"999\nDXF created by Tekla\n  0\nSECTION\n", "part.txt", None, FileKind.DXF),
        (b"AutoCAD Binary DXF\r\n\x1a\x00", "part.dxf", None, FileKind.DXF),
        (
            b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC2X3'));\nENDSEC;\n",
            "model.dat",
            None,
            FileKind.IFC,
        ),
        (
            b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));",
            "p.stp",
            None,
            FileKind.STEP,
        ),
        (b"ISO-10303-21;\nHEADER;\n" + b"x" * 9000, "big.ifc", None, FileKind.IFC),
        (b" " * 72 + b"S      1\n", "shape.igs", None, FileKind.IGES),
        (b"#LcUStream\x00\x01", "site.nwd", None, FileKind.NAVISWORKS),
        (b"SCHNEIDER\x00\x01", "relay.zm2", None, FileKind.PLC_PROGRAM),
        (
            b"PK\x03\x04",
            "m.smlx",
            ["_rels/.rels", "Contents/M.xml", "SatFiles/32.sat", "DSTV/NC/B1.nc"],
            FileKind.STEEL_MODEL,
        ),
        (b"PK\x03\x04", "m.ifczip", ["model.ifc"], FileKind.IFC_ZIP),
    ],
)
def test_sniff_uses_bytes_not_names(head, name, entries, kind) -> None:
    assert sniff(head, name=name, zip_entries=entries).kind is kind


def test_cad_text_formats_are_never_plain_text() -> None:
    # Before signature sniffing these fell through to the text route and were embedded.
    for head in (b"  0\nSECTION\n", b"ISO-10303-21;\nHEADER;\n"):
        assert sniff(head, name="x.txt").kind is not FileKind.TEXT


def test_sniff_flags_extension_mismatch() -> None:
    sniffed = sniff(b"%PDF-1.4", name="rapport.xlsx")
    assert sniffed.kind is FileKind.PDF
    assert sniffed.mismatch


@pytest.mark.parametrize(
    ("entries", "compressed"),
    [
        ([("a.xml", 10, 10_000)], 10),
        ([(f"f{i}", 1, 1) for i in range(5)], 100),
        ([("../evil", 1, 1)], 100),
    ],
)
def test_zip_safety_rejects_bombs_and_unsafe_paths(entries, compressed) -> None:
    limits = ZipLimits(max_entries=4, max_ratio=100)
    with pytest.raises(UnsupportedContentError):
        check_zip_safety(entries, compressed_size=compressed, limits=limits)


def test_page_classification() -> None:
    policy = RoutingPolicy()
    assert classify_page(page(1), policy) is Route.NATIVE_PDF_TEXT
    assert classify_page(page(1, chars=0), policy) is Route.BLANK
    assert classify_page(page(1, chars=0, images=1), policy) is Route.DI_LAYOUT
    assert classify_page(page(1, chars=5, paths=40), policy) is Route.DI_LAYOUT
    assert classify_page(page(1, paths=40, size=(1191, 842)), policy) is Route.DI_LAYOUT_DRAWING
    noisy = page(1).model_copy(update={"garbage_ratio": 0.5})
    assert classify_page(noisy, policy) is Route.DI_LAYOUT


def test_pdf_units_cover_every_page_in_order() -> None:
    pages = [
        page(1),
        page(2),
        page(3, chars=0),
        page(4, chars=0, images=1),
        page(5, chars=0, paths=50, size=(1191, 842)),
        page(6, chars=0, paths=50, size=(1191, 842)),
        page(7),
    ]
    units = plan_units(report(pages), RoutingPolicy())
    assert [(unit.route, unit.pages) for unit in units] == [
        (Route.NATIVE_PDF_TEXT, (1, 2)),
        (Route.BLANK, (3,)),
        (Route.DI_LAYOUT, (4,)),
        (Route.DI_LAYOUT_DRAWING, (5,)),
        (Route.DI_LAYOUT_DRAWING, (6,)),
        (Route.NATIVE_PDF_TEXT, (7,)),
    ]
    assert units[1].disposition is UnitDisposition.NO_EVIDENCE
    assert units[3].kind is UnitKind.DRAWING_SHEET
    assert units[2].locator == {
        "submit": SubmitMode.PAGES.value, "max_submit_bytes": RoutingPolicy().max_submit_bytes,
    }


def test_provider_limits_also_bound_pdf_submissions() -> None:
    pages = [page(number, chars=0, images=1) for number in range(1, 7)]
    policy = RoutingPolicy(max_submit_bytes=10_000, di_max_bytes=500, di_max_pages=2)
    units = plan_units(report(pages, size=600), policy)
    assert [unit.pages for unit in units] == [(1, 2), (3, 4), (5, 6)]
    assert all(unit.locator == {"submit": "subset", "max_submit_bytes": 500} for unit in units)
    page_limited = plan_units(report(pages, size=100), policy)
    assert all(unit.locator["submit"] == "subset" for unit in page_limited)


def test_large_sources_submit_subsets_and_layout_units_are_bounded() -> None:
    pages = [page(number, chars=0, images=1) for number in range(1, 46)]
    policy = RoutingPolicy(max_submit_bytes=100)
    units = plan_units(report(pages, size=10_000), policy)
    assert [len(unit.pages) for unit in units] == [20, 20, 5]
    assert {unit.locator["submit"] for unit in units} == {SubmitMode.SUBSET.value}


def test_disabled_routes_become_unsupported_units() -> None:
    policy = RoutingPolicy(enabled_routes=frozenset({Route.NATIVE_PDF_TEXT}))
    units = plan_units(report([page(1), page(2, chars=0, images=1)]), policy)
    assert units[0].disposition is None
    assert units[1].disposition is UnitDisposition.UNSUPPORTED
    assert "disabled" in (units[1].reason or "")


@pytest.mark.parametrize(
    ("family", "kind", "route", "disposition"),
    [
        (Family.SPREADSHEET, FileKind.XLSX, Route.NATIVE_SPREADSHEET, None),
        (Family.TEXT, FileKind.TEXT, Route.NATIVE_TEXT, None),
        (Family.OFFICE, FileKind.DOCX, Route.DI_LAYOUT_OFFICE, None),
        (Family.IMAGE, FileKind.PNG, Route.DI_LAYOUT, None),
        (Family.ARCHIVE, FileKind.ZIP, Route.UNSUPPORTED, UnitDisposition.UNSUPPORTED),
        (Family.EXECUTABLE, FileKind.EXECUTABLE, Route.UNSUPPORTED, UnitDisposition.UNSUPPORTED),
        (Family.BIM, FileKind.IFC, Route.NATIVE_IFC, None),
        (Family.CAD, FileKind.DWG, Route.NATIVE_DWG, None),
        (Family.CAD, FileKind.STEP, Route.UNSUPPORTED, UnitDisposition.UNSUPPORTED),
    ],
)
def test_whole_file_units(family, kind, route, disposition) -> None:
    [unit] = plan_units(report(family=family, kind=kind), RoutingPolicy())
    assert unit.route is route
    assert unit.disposition is disposition


def test_cad_sources_are_recorded_as_lineage_only() -> None:
    [unit] = plan_units(report(family=Family.CAD, kind=FileKind.DXF), RoutingPolicy())
    assert unit.reason == "lineage only: dxf is recorded without extraction"
    [model] = plan_units(report(family=Family.BIM, kind=FileKind.IFC), RoutingPolicy())
    assert model.kind is UnitKind.BIM_MODEL


def test_plan_identity_ignores_placement_but_not_policy() -> None:
    base = report([page(1)])
    moved = base.model_copy(
        update={"snapshot_id": "snap-2", "blob_name": "Archive/copie.pdf", "folder_hint": "Archive"}
    )
    policy = RoutingPolicy()
    assert plan_id_for(base, policy) == plan_id_for(moved, policy)
    assert plan_id_for(base, policy) != plan_id_for(base, RoutingPolicy(min_text_chars=10))


def test_projection_keys_never_start_with_underscore() -> None:
    import base64
    import hashlib

    from ingest_pipeline.domain.chunks import projection_key

    def raw(chunk: str) -> str:
        digest = hashlib.sha256(f"r|{chunk}".encode()).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    underscored = next(f"c{i}" for i in range(10_000) if raw(f"c{i}").startswith("_"))
    key = projection_key("r", underscored)
    assert key.startswith("k")
    assert key[1:] == raw(underscored)[1:]
    assert projection_key("r", "c0") == raw("c0") or raw("c0").startswith("_")
    assert all(not projection_key("r", f"c{i}").startswith("_") for i in range(2_000))


def test_page_ranges() -> None:
    assert page_ranges([3, 4, 5, 7, 9, 10]) == "3-5,7,9-10"
    assert page_ranges([1]) == "1"
    assert page_ranges([]) == ""


def test_locators_validate_and_compact() -> None:
    with pytest.raises(ValueError, match="missing unit"):
        Locator(kind=LocatorKind.REGION, page=1)
    cell = Locator(kind=LocatorKind.CELL, sheet="Feuil1", cell="B2", cell_end="D9", row=2, column=2)
    assert cell.compact() == "cell:Feuil1!B2:D9"
    region = Locator(kind=LocatorKind.REGION, page=3, unit="inch", polygon=(1, 2, 3, 2, 3, 4, 1, 4))
    assert region.compact() == "region:p3:inch:1,2,3,4"
    assert Locator(kind=LocatorKind.DOCUMENT, sheet="A3 Advance Steel").compact() == (
        "sheet:A3 Advance Steel"
    )
    assert Locator(kind=LocatorKind.DOCUMENT).compact() == "document"


def test_table_grid_repeats_spanned_cells() -> None:
    table = EvidenceTable(
        row_count=2,
        column_count=3,
        header_rows=1,
        cells=(
            EvidenceCell(row=0, column=0, column_span=2, kind=CellKind.COLUMN_HEADER, text="Debit"),
            EvidenceCell(row=0, column=2, kind=CellKind.COLUMN_HEADER, text="Unite"),
            EvidenceCell(row=1, column=0, text="45"),
            EvidenceCell(row=1, column=2, text="m3/h"),
        ),
    )
    assert table.grid() == [["Debit", "Debit", "Unite"], ["45", "", "m3/h"]]
    assert table.cell_at(0, 1) is table.cells[0]


def test_blocks_require_matching_payloads() -> None:
    with pytest.raises(ValueError, match="carry \\(and require\\) a table"):
        EvidenceBlock(evidence_id="b", kind=BlockKind.TABLE, text="x")
    with pytest.raises(ValueError, match="Headings need a level"):
        EvidenceBlock(evidence_id="b", kind=BlockKind.HEADING, text="x")


def test_text_helpers() -> None:
    assert normalize_text("e\u0301te\r\nligne \x07\r") == "\u00e9te\nligne"
    assert detect_language("La pompe est dans le local et les vannes sont pour la ligne") == "fr"
    assert detect_language("The pump is in the room and the valves are for the line") == "en"
    assert detect_language("P-201") is None
    assert a1(2, 2) == "B2"
    assert a1(1, 28) == "AB1"
