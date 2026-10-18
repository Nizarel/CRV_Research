"""Identify file types from leading bytes (and container entries), never from names alone."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from .errors import UnsupportedContentError


class FileKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    XLSB = "xlsb"
    PPTX = "pptx"
    ODS = "ods"
    XLS = "xls"
    CFB_OTHER = "cfb"
    ZIP = "zip"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    BMP = "bmp"
    GIF = "gif"
    HEIF = "heif"
    HTML = "html"
    TEXT = "text"
    IFC = "ifc"
    IFC_ZIP = "ifczip"
    STEP = "step"
    IGES = "iges"
    DWG = "dwg"
    DXF = "dxf"
    NAVISWORKS = "navisworks"
    PLC_PROGRAM = "plc_program"
    """Schneider Electric program file (``.zm2``)."""
    STEEL_MODEL = "steel_model"
    """Advance Steel package (``.smlx``): model XML, SAT geometry and DSTV NC files."""
    EXECUTABLE = "executable"
    EMPTY = "empty"
    UNKNOWN = "unknown"


EXTENSION_KINDS: dict[str, FileKind] = {
    ".pdf": FileKind.PDF,
    ".docx": FileKind.DOCX,
    ".docm": FileKind.DOCX,
    ".xlsx": FileKind.XLSX,
    ".xlsm": FileKind.XLSX,
    ".xlsb": FileKind.XLSB,
    ".pptx": FileKind.PPTX,
    ".pptm": FileKind.PPTX,
    ".ods": FileKind.ODS,
    ".xls": FileKind.XLS,
    ".doc": FileKind.CFB_OTHER,
    ".ppt": FileKind.CFB_OTHER,
    ".msg": FileKind.CFB_OTHER,
    ".zip": FileKind.ZIP,
    ".png": FileKind.PNG,
    ".jpg": FileKind.JPEG,
    ".jpeg": FileKind.JPEG,
    ".tif": FileKind.TIFF,
    ".tiff": FileKind.TIFF,
    ".bmp": FileKind.BMP,
    ".gif": FileKind.GIF,
    ".heic": FileKind.HEIF,
    ".heif": FileKind.HEIF,
    ".html": FileKind.HTML,
    ".htm": FileKind.HTML,
    ".txt": FileKind.TEXT,
    ".csv": FileKind.TEXT,
    ".tsv": FileKind.TEXT,
    ".md": FileKind.TEXT,
    ".log": FileKind.TEXT,
    ".exe": FileKind.EXECUTABLE,
    ".dll": FileKind.EXECUTABLE,
    ".ifc": FileKind.IFC,
    ".ifczip": FileKind.IFC_ZIP,
    ".step": FileKind.STEP,
    ".stp": FileKind.STEP,
    ".igs": FileKind.IGES,
    ".iges": FileKind.IGES,
    ".dwg": FileKind.DWG,
    ".dxf": FileKind.DXF,
    ".nwd": FileKind.NAVISWORKS,
    ".nwc": FileKind.NAVISWORKS,
    ".zm2": FileKind.PLC_PROGRAM,
    ".smlx": FileKind.STEEL_MODEL,
}

CFB_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
SNIFF_BYTES = 8192
_DXF_ASCII = re.compile(rb"^\s*(?:999\s*\r?\n[^\r\n]*\r?\n\s*)?0\s*\r?\n\s*SECTION\b")
_STEP_IFC_SCHEMA = re.compile(rb"FILE_SCHEMA\s*\(\s*\(\s*'IFC", re.IGNORECASE)


@dataclass(frozen=True)
class Sniffed:
    kind: FileKind
    extension: str
    declared: FileKind | None
    """Kind implied by the extension, if the extension is known."""

    @property
    def mismatch(self) -> bool:
        return self.declared is not None and self.declared is not self.kind


def extension_of(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def needs_container_listing(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04")


def sniff(
    head: bytes,
    *,
    name: str,
    zip_entries: Sequence[str] | None = None,
    odf_mimetype: str | None = None,
) -> Sniffed:
    """Classify ``head`` (the first ``SNIFF_BYTES``); zip containers need their entry names."""

    extension = extension_of(name)
    declared = EXTENSION_KINDS.get(extension)
    return Sniffed(_kind(head, extension, zip_entries, odf_mimetype), extension, declared)


def _kind(
    head: bytes, extension: str, entries: Sequence[str] | None, odf_mimetype: str | None
) -> FileKind:
    if not head:
        return FileKind.EMPTY
    if head.lstrip(b"\x00\t\r\n ").startswith(b"%PDF-") or b"%PDF-" in head[:1024]:
        return FileKind.PDF
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(entries or (), odf_mimetype)
    if head.startswith(CFB_SIGNATURE):
        return FileKind.XLS if extension == ".xls" else FileKind.CFB_OTHER
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind.PNG
    if head.startswith(b"\xff\xd8\xff"):
        return FileKind.JPEG
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return FileKind.TIFF
    if head.startswith(b"BM"):
        return FileKind.BMP
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return FileKind.GIF
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif"):
        return FileKind.HEIF
    if head.startswith(b"MZ"):
        return FileKind.EXECUTABLE
    cad = _cad_kind(head, extension)
    if cad is not None:
        return cad
    return _text_kind(head)


def _cad_kind(head: bytes, extension: str) -> FileKind | None:
    """CAD/BIM formats; the text-based ones would otherwise be read as plain text."""

    if head[:4] == b"AC10" and head[4:6].isdigit():
        return FileKind.DWG
    if head.startswith(b"AutoCAD Binary DXF") or _DXF_ASCII.match(head):
        return FileKind.DXF
    if head.lstrip().startswith(b"ISO-10303-21"):
        is_ifc = _STEP_IFC_SCHEMA.search(head) or extension == ".ifc"
        return FileKind.IFC if is_ifc else FileKind.STEP
    if head.startswith(b"#LcUStre"):
        return FileKind.NAVISWORKS
    if head.startswith(b"SCHNEIDE"):
        return FileKind.PLC_PROGRAM
    # IGES has no magic: 80-column records with the section letter in column 73.
    if extension in (".igs", ".iges") and head[72:73] == b"S":
        return FileKind.IGES
    return None


def _zip_kind(entries: Sequence[str], odf_mimetype: str | None) -> FileKind:
    names = set(entries)
    if odf_mimetype == "application/vnd.oasis.opendocument.spreadsheet":
        return FileKind.ODS
    if "[Content_Types].xml" in names:
        if "word/document.xml" in names:
            return FileKind.DOCX
        if "xl/workbook.bin" in names:
            return FileKind.XLSB
        if "xl/workbook.xml" in names:
            return FileKind.XLSX
        if "ppt/presentation.xml" in names:
            return FileKind.PPTX
    if any(name.startswith("SatFiles/") for name in names) and any(
        name.startswith("Contents/") and name.endswith(".xml") for name in names
    ):
        return FileKind.STEEL_MODEL
    files = [name for name in names if not name.endswith("/")]
    if files and all(name.lower().endswith(".ifc") for name in files):
        return FileKind.IFC_ZIP
    return FileKind.ZIP


def _text_kind(head: bytes) -> FileKind:
    if b"\x00" in head:
        return FileKind.UNKNOWN
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        # A multi-byte character can straddle the sniff boundary.
        try:
            text = head[:-3].decode("utf-8-sig")
        except UnicodeDecodeError:
            text = head.decode("cp1252", errors="replace")
    printable = sum(char.isprintable() or char in "\r\n\t" for char in text)
    if not text or printable / len(text) < 0.95:
        return FileKind.UNKNOWN
    lowered = text.lstrip().lower()
    if lowered.startswith(("<!doctype html", "<html")):
        return FileKind.HTML
    return FileKind.TEXT


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 20_000
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_ratio: float = 200.0


def check_zip_safety(
    entries: Sequence[tuple[str, int, int]], *, compressed_size: int, limits: ZipLimits
) -> None:
    """Reject decompression bombs before a parser opens an OOXML/ODS container.

    ``entries`` are ``(name, compressed_bytes, uncompressed_bytes)`` from the central directory.
    """

    if len(entries) > limits.max_entries:
        raise UnsupportedContentError(f"Container has {len(entries)} entries")
    total = sum(max(uncompressed, 0) for _, _, uncompressed in entries)
    if total > limits.max_uncompressed_bytes:
        raise UnsupportedContentError(f"Container expands to {total} bytes")
    if compressed_size > 0 and total / compressed_size > limits.max_ratio:
        raise UnsupportedContentError("Container compression ratio looks like a zip bomb")
    if any(name.startswith(("/", "\\")) or ".." in name.split("/") for name, _, _ in entries):
        raise UnsupportedContentError("Container has unsafe entry paths")
