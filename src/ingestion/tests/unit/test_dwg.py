"""DWG route: converter sandbox, DXF reader and evidence normalizer."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest
from fixture_builders import (
    DWG_BYTES,
    FAKE_DWG2DXF,
    FRAME_LINES,
    SHOP_LAYOUT,
    shop_drawing_dxf,
)

from ingest_pipeline.adapters.parsers.libredwg import (
    ConverterLimits,
    LibreDwgReader,
    limited,
    read_dxf,
)
from ingest_pipeline.domain.errors import ConfigurationError, UnsupportedContentError
from ingest_pipeline.domain.evidence import BlockKind
from ingest_pipeline.domain.models import UnitRecord
from ingest_pipeline.domain.routing import Route, UnitKind
from ingest_pipeline.ports.parsing import DrawingData, DrawingText
from ingest_pipeline.stages.normalizers.dwg import normalize_dwg, rows

HASH = "d" * 64


def _unit() -> UnitRecord:
    return UnitRecord(
        unit_id="unit:dwg:00000",
        ordinal=0,
        kind=UnitKind.CAD_DRAWING.value,
        route=Route.NATIVE_DWG.value,
    )


def _reader(mode: str, **limits: float) -> LibreDwgReader:
    return LibreDwgReader(
        ConverterLimits(**limits),  # type: ignore[arg-type]
        command=(sys.executable, str(FAKE_DWG2DXF), mode),
        resource_limits=False,
    )


@pytest.fixture
def dwg(tmp_path: Path) -> Path:
    path = tmp_path / "part.dwg"
    path.write_bytes(DWG_BYTES)
    return path


def _text(x: float, y: float, text: str, height: float = 2.4) -> DrawingText:
    return DrawingText(layout="L", entity="TEXT", text=text, x=x, y=y, height=height)


def test_rows_rebuild_drawn_tables() -> None:
    texts = [
        _text(62.1, 9.6, "Quantity"),
        _text(49.5, 9.6, "Mark"),
        _text(91.6, 9.6, "Profile"),
        _text(50.3, 5.4, "B39"),
        _text(67.2, 5.4, "1"),
        _text(76.9, 5.4, "IPE270"),
        _text(156.7, -119.4, "1", 5.0),
        _text(163.0, -119.4, "- OFF REQUIRED", 5.6),
        _text(228.7, -119.4, "IPE270 - MARKED - B39", 5.0),
    ]
    assert rows(texts) == [
        "Mark | Quantity | Profile",
        "B39 | 1 | IPE270",
        "1 - OFF REQUIRED | IPE270 - MARKED - B39",
    ]


def test_limits_wrap_the_converter() -> None:
    limits = ConverterLimits(timeout_seconds=59.5, memory_bytes=1024, output_bytes=2048)
    assert limited(["dwg2dxf", "-v0", "a.dwg"], limits) == [
        "prlimit",
        "--data=1024",
        "--cpu=60",
        "--fsize=2048",
        "--core=0",
        "--",
        "dwg2dxf",
        "-v0",
        "a.dwg",
    ]


def test_reader_keeps_text_and_attributes_and_counts_geometry(tmp_path: Path) -> None:
    drawing = read_dxf(shop_drawing_dxf(tmp_path / "part.dxf"), "dwg2dxf 0.14")
    assert drawing.layouts == ("Model", SHOP_LAYOUT)
    assert drawing.solids == 0
    assert drawing.codepage == "ANSI_1252"
    title = next(i for i in drawing.inserts if i.block == "HYPERSTEELPAGEHEADER")
    assert title.layout == SHOP_LAYOUT
    assert dict(title.attributes)["POS_NUM"] == "B39"
    model = {text.text for text in drawing.texts if text.layout == "Model"}
    assert "IPE270 - MARKED - B39 1 - OFF REQUIRED" in model
    assert "659 OVERALL" in model
    assert len([i for i in drawing.inserts if i.block == "BOLT"]) == 3


def test_normalized_drawing_sets_frame_aside_and_exposes_fields(tmp_path: Path) -> None:
    drawing = read_dxf(shop_drawing_dxf(tmp_path / "part.dxf"), "dwg2dxf 0.14")
    evidence = normalize_dwg(drawing, unit=_unit(), plan_id="plan:t", content_hash=HASH)
    assert evidence.extraction_method == "native_dwg"
    assert evidence.provider["converter"] == "dwg2dxf 0.14"
    kinds = [block.kind for block in evidence.blocks]
    assert kinds[0] is BlockKind.TITLE
    assert kinds.count(BlockKind.HEADING) == 2
    texts = [block.text for block in evidence.blocks]
    assert (
        "Title block HYPERSTEELPAGEHEADER: DETAILER: MSZ; DATE_DRAW: 2024/06/26; "
        "PROJECT: 6520-CV-5237; PROJECT_NO: CV-5237; POS_NUM: B39; SCALE: 1:5; "
        "REVISION_INDEX: 02; ROLE: Column"
    ) in texts
    sheet = next(
        b.text for b in evidence.blocks if b.kind is BlockKind.PARAGRAPH and "Mark" in b.text
    )
    assert sheet.splitlines() == [
        "02 | 31-Jul-24 | DRAWING UPDATED TO SUIT COMMENTS",
        "Index | Date | Description",
        "Mark | Quantity | Profile | Length | Material | Weight",
        "B39 | 1 | IPE270 | 659 | S275JR | 23.79",
    ]
    [footer] = [b for b in evidence.blocks if b.kind is BlockKind.PAGE_FOOTER]
    assert footer.text == "; ".join(FRAME_LINES)
    assert all("Greenstone" not in b.text for b in evidence.blocks if b is not footer)
    bolts = next(b.table for b in evidence.blocks if b.table is not None)
    assert bolts.grid() == [["SIZE", "GRADE", "Count"], ["M20", "8.8", "2"], ["M16", "8.8", "1"]]
    assert {loc.compact() for b in evidence.blocks for loc in b.locators} == {
        "document",
        "sheet:Model",
        f"sheet:{SHOP_LAYOUT}",
    }
    fields = {(item.path, item.value) for item in evidence.fields}
    assert ("TitleBlock[0].POS_NUM", "B39") in fields
    assert ("TitleBlock[0].REVISION_INDEX", "02") in fields
    assert ("EquipmentTags[0]", "6520-CV-5237") in fields
    assert {item.method for item in evidence.fields} == {"native"}
    assert not evidence.warnings


def test_drawings_without_real_text_have_no_content() -> None:
    frame_only = DrawingData(
        converter="c",
        dxf_version="AC1032",
        codepage=None,
        layouts=("Model",),
        entities=3,
        texts=(_text(0, 0, "Building 12"), _text(0, 5, "A3")),
        inserts=(),
        solids=4,
    )
    evidence = normalize_dwg(frame_only, unit=_unit(), plan_id="p", content_hash=HASH)
    assert not evidence.blocks
    assert not evidence.has_content
    assert evidence.warnings == ("4 3D solids or surfaces: geometry is not read",)


async def test_converter_output_is_read(tmp_path: Path, dwg: Path) -> None:
    dxf = shop_drawing_dxf(tmp_path / "prepared.dxf")
    work = tmp_path / "work"
    work.mkdir()
    drawing = await _reader(f"copy:{dxf}").read(dwg, work)
    assert drawing.converter == "dwg2dxf 0.14 (fake)"
    assert SHOP_LAYOUT in drawing.layouts


async def test_hung_converter_is_killed(tmp_path: Path, dwg: Path) -> None:
    started = time.monotonic()
    with pytest.raises(UnsupportedContentError, match="exceeded 2 s"):
        await _reader("sleep", timeout_seconds=2).read(dwg, tmp_path)
    assert time.monotonic() - started < 20


@pytest.mark.parametrize(
    ("mode", "message"),
    [("fail", "exit 1.*boom"), ("garbage", "DXF output is unreadable")],
)
async def test_failed_conversions_are_unsupported(
    tmp_path: Path, dwg: Path, mode: str, message: str
) -> None:
    with pytest.raises(UnsupportedContentError, match=message):
        await _reader(mode).read(dwg, tmp_path)


async def test_missing_or_broken_converter_is_a_configuration_error(
    tmp_path: Path, dwg: Path
) -> None:
    with pytest.raises(ConfigurationError, match="not installed"):
        await LibreDwgReader(command=("no-such-dwg2dxf",)).read(dwg, tmp_path)
    with pytest.raises(ConfigurationError, match="broken"):
        await _reader("broken").read(dwg, tmp_path)


@pytest.mark.skipif(shutil.which("prlimit") is None, reason="prlimit is Linux-only")
async def test_resource_limits_wrap_a_real_conversion(tmp_path: Path, dwg: Path) -> None:
    dxf = shop_drawing_dxf(tmp_path / "prepared.dxf")
    reader = LibreDwgReader(command=(sys.executable, str(FAKE_DWG2DXF), f"copy:{dxf}"))
    drawing = await reader.read(dwg, tmp_path)
    assert drawing.inserts
