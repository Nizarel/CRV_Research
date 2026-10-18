"""Deterministic builders for tiny PDF and XLSX fixtures (no binary files in the repository)."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

import pytest

A4 = (595.0, 842.0)
A3_LANDSCAPE = (1191.0, 842.0)


def repo_file(relative: str) -> Path:
    """A reviewed repository file (checkout or test image); skip when it is not shipped."""

    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} is not available here")


INDEX_SCHEMA = "infra/search/evidence-index-v2.json"


def index_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(repo_file(INDEX_SCHEMA).read_text(encoding="utf-8")))


def index_fields() -> frozenset[str]:
    return frozenset(item["name"] for item in index_schema()["fields"])


@dataclass
class PdfPage:
    lines: list[str] = field(default_factory=list)
    size: tuple[float, float] = A4
    rotate: int = 0
    vector_lines: int = 0
    """Number of stroked line segments (a 'drawing' without a text layer)."""
    image: bool = False
    """Draw one tiny raster image (a 'scanned' page without a text layer)."""


def _pdf_string(text: str) -> str:
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def pdf_bytes(pages: list[PdfPage], *, labels: list[str] | None = None) -> bytes:
    """Write a valid PDF with Helvetica text, vector paths, images and /Rotate per page."""

    objects: dict[int, bytes] = {}
    font_id, image_id = 3, 4
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects[image_id] = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
    )
    kids: list[int] = []
    next_id = 5
    for page in pages:
        content = io.StringIO()
        if page.lines:
            content.write("BT /F1 11 Tf 14 TL 56 780 Td\n")
            for line in page.lines:
                content.write(f"{_pdf_string(line)} Tj T*\n")
            content.write("ET\n")
        for index in range(page.vector_lines):
            content.write(f"{50 + index} 50 m {500 + index} 700 l S\n")
        if page.image:
            content.write("q 200 0 0 200 100 300 cm /Im1 Do Q\n")
        stream = content.getvalue().encode("latin-1")
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream"
        )
        width, height = page.size
        rotate = f" /Rotate {page.rotate}" if page.rotate else ""
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:g} {height:g}]{rotate}"
            f" /Resources << /Font << /F1 {font_id} 0 R >> /XObject << /Im1 {image_id} 0 R >> >>"
            f" /Contents {content_id} 0 R >>"
        ).encode()
        kids.append(page_id)
    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if labels:
        nums = " ".join(
            f"{index} << /P {_pdf_string(label)} >>" for index, label in enumerate(labels)
        )
        catalog += f" /PageLabels << /Nums [{nums}] >>"
    objects[1] = (catalog + " >>").encode()
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] /Count {len(kids)} >>"
    ).encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = out.tell()
        out.write(f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n")
    xref = out.tell()
    size = max(objects) + 1
    out.write(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for object_id in range(1, size):
        out.write(f"{offsets[object_id]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


@dataclass
class Sheet:
    name: str
    cells: dict[str, object]
    """A1 address -> str, int, float, bool, ``('=FORMULA', cached)`` or ``('date', serial)``."""
    state: str = "visible"
    merged: list[str] = field(default_factory=list)


def xlsx_bytes(sheets: list[Sheet]) -> bytes:
    """Minimal SpreadsheetML workbook with shared strings, a date style and merged cells."""

    strings: list[str] = []

    def shared(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    def cell_xml(ref: str, value: object) -> str:
        if isinstance(value, tuple) and value[0] == "date":
            return f'<c r="{ref}" s="1"><v>{value[1]}</v></c>'
        if isinstance(value, tuple):
            formula, cached = value
            return f'<c r="{ref}"><f>{escape(str(formula).lstrip("="))}</f><v>{cached}</v></c>'
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
        if isinstance(value, int | float):
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="s"><v>{shared(str(value))}</v></c>'

    def row_of(ref: str) -> int:
        return int("".join(char for char in ref if char.isdigit()))

    def col_of(ref: str) -> int:
        number = 0
        for char in ref:
            if char.isalpha():
                number = number * 26 + ord(char.upper()) - 64
        return number

    sheet_files: list[str] = []
    for sheet in sheets:
        rows: dict[int, list[tuple[str, object]]] = {}
        for ref, value in sheet.cells.items():
            rows.setdefault(row_of(ref), []).append((ref, value))
        body = "".join(
            f'<row r="{row}">'
            + "".join(
                cell_xml(ref, value)
                for ref, value in sorted(cells, key=lambda c: col_of(c[0]))
            )
            + "</row>"
            for row, cells in sorted(rows.items())
        )
        merged = ""
        if sheet.merged:
            merged = f'<mergeCells count="{len(sheet.merged)}">' + "".join(
                f'<mergeCell ref="{ref}"/>' for ref in sheet.merged
            ) + "</mergeCells>"
        sheet_files.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{body}</sheetData>{merged}</worksheet>"
        )

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{main}"'
        f' xmlns:r="{rel_ns}"><sheets>'
        + "".join(
            f'<sheet name="{escape(sheet.name)}" sheetId="{index + 1}" r:id="rId{index + 1}"'
            + (f' state="{sheet.state}"' if sheet.state != "visible" else "")
            + "/>"
            for index, sheet in enumerate(sheets)
        )
        + "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index + 1}" Type="{rel_ns}/worksheet"'
            f' Target="worksheets/sheet{index + 1}.xml"/>'
            for index in range(len(sheets))
        )
        + f'<Relationship Id="rIdS" Type="{rel_ns}/sharedStrings" Target="sharedStrings.xml"/>'
        + f'<Relationship Id="rIdT" Type="{rel_ns}/styles" Target="styles.xml"/>'
        + "</Relationships>"
    )
    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{main}">'
        '<fonts count="1"><font/></fonts><fills count="1"><fill/></fills>'
        '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0"/>'
        '<xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
        "</styleSheet>"
    )
    shared_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="{main}"'
        f' count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{escape(text)}</t></si>" for text in strings)
        + "</sst>"
    )
    sml = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    package_rels = "application/vnd.openxmlformats-package.relationships+xml"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Default Extension="rels" ContentType="{package_rels}"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{sml}.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index + 1}.xml"'
            f' ContentType="{sml}.worksheet+xml"/>'
            for index in range(len(sheets))
        )
        + f'<Override PartName="/xl/sharedStrings.xml" ContentType="{sml}.sharedStrings+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{sml}.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in [
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", root_rels),
            ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", rels),
            ("xl/styles.xml", styles),
            ("xl/sharedStrings.xml", shared_xml),
            *[
                (f"xl/worksheets/sheet{index + 1}.xml", xml)
                for index, xml in enumerate(sheet_files)
            ],
        ]:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return out.getvalue()


FAKE_DWG2DXF = Path(__file__).with_name("fake_dwg2dxf.py")
DWG_BYTES = b"AC1032" + bytes(122)
"""Signature of an AutoCAD 2018 DWG: enough for the sniffer; the fake converter ignores it."""
SHOP_LAYOUT = "A3 Advance Steel"
FRAME_LINES = (
    "Building 12",
    "Greenstone Hill Office Park",
    "Emerald Boulevard",
    "Greenstone, 1609.",
    "Republic Of South Africa",
    "Email: Drawingoffice@senet.co.za",
)
TITLE_BLOCK = {
    "DETAILER": "MSZ",
    "DATE_DRAW": "2024/06/26",
    "PROJECT": "6520-CV-5237",
    "PROJECT_NO": "CV-5237",
    "POS_NUM": "B39",
    "SCALE": "1:5",
    "REVISION_INDEX": "02",
    "ROLE": "Column",
}


def shop_drawing_dxf(path: Path) -> Path:
    """An Advance Steel-like part sheet: title block, drawn BOM, revision row and frame text.

    Positions are those of a pilot drawing, so rows rebuild the way they do on real sheets.
    """

    import ezdxf

    doc = ezdxf.new("R2018")
    header = doc.blocks.new("HYPERSTEELPAGEHEADER")
    for index, tag in enumerate(TITLE_BLOCK):
        header.add_attdef(tag, (0, -3.0 * index), dxfattribs={"height": 2.5})
    bolt = doc.blocks.new("BOLT")
    bolt.add_attdef("SIZE", (0, 0))
    bolt.add_attdef("GRADE", (0, -2))
    sheet = doc.layouts.new(SHOP_LAYOUT)
    sheet.add_blockref("HYPERSTEELPAGEHEADER", (452.4, 0)).add_auto_attribs(TITLE_BLOCK)
    drawn = [
        (49.5, 9.6, "Mark"), (62.1, 9.6, "Quantity"), (91.6, 9.6, "Profile"),
        (121.3, 9.6, "Length"), (144.8, 9.6, "Material"), (194.6, 9.6, "Weight"),
        (50.3, 5.4, "B39"), (67.2, 5.4, "1"), (76.9, 5.4, "IPE270"),
        (129.1, 5.4, "659"), (144.9, 5.4, "S275JR"), (201.3, 5.4, "23.79"),
        (295.6, 37.9, "Index"), (313.7, 37.9, "Date"), (371.2, 37.9, "Description"),
        (298.0, 43.4, "02"), (310.1, 43.4, "31-Jul-24"),
        (329.4, 43.4, "DRAWING UPDATED TO SUIT COMMENTS"),
    ]
    for x, y, text in drawn:
        sheet.add_text(text, dxfattribs={"height": 2.4, "insert": (x, y)})
    for index, line in enumerate(FRAME_LINES):
        sheet.add_text(line, dxfattribs={"height": 0.79, "insert": (354.5, 31.4 - 1.25 * index)})
    model = doc.modelspace()
    model.add_mtext("IPE270 - MARKED - B39\\P1 - OFF REQUIRED", dxfattribs={"char_height": 5})
    model.add_linear_dim(base=(0, 20), p1=(0, 0), p2=(659, 0), text="659 OVERALL").render()
    for size, grade in (("M20", "8.8"), ("M20", "8.8"), ("M16", "8.8")):
        model.add_blockref("BOLT", (0, 0)).add_auto_attribs({"SIZE": size, "GRADE": grade})
    doc.saveas(path)
    return path
