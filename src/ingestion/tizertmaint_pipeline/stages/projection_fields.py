"""Search-row metadata from the placements' paths and the units' extracted fields (index v2).

One row serves every placement of the same bytes: the canonical placement names the row, and the
collection fields list them all. Path-derived values are *reported* context (the corpus is filed
by equipment dossier); printed values come from extracted fields or the chunk text.
``equipment_tags`` (the backend's tag filter) is their union: a retrieval scope, not an approved
equipment association. Approved associations belong to knowledge-graph releases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..domain.chunks import ChunkKind, EvidenceChunk
from ..domain.document_classes import document_class
from ..domain.evidence import EvidenceKind, FieldObservation
from .normalizers.ifc import DOCUMENT, TAG

MAX_IDENTIFIERS = 64
MAX_TITLE_CHARS = 200
MODEL_SPACE = "sheet:Model"
_NUMBERED = re.compile(r"^\d+\s*-\s*")
_DRAWING_KINDS = frozenset({ChunkKind.DRAWING_TEXT, ChunkKind.DRAWING_TABLE})
_CODE = re.compile(r"(?=[A-Z.\-/]*\d)[A-Z0-9]+(?:[-./][A-Z0-9]+)+")
_REVISION = re.compile(r"[A-Z0-9]{1,3}", re.IGNORECASE)
_PAPER_SIZE = re.compile(r"A[0-4]", re.IGNORECASE)
_ISSUE_STATUS = re.compile(
    r"\b(?:ISSUED?|CONSTRUCTION|APPROV\w*|COMMENTS?|REVIEW|INFORMATION|DESIGN|TENDER|BUILT|"
    r"PRELIMINARY|FINAL)\b|^(?:IF[A-Z]|AFC)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlacementMetadata:
    zone_name: str | None
    source_path: str
    """Readable path for full-text search: ``_`` would glue tokens like ``N°2_6520``."""
    equipment_folder: str | None
    """The shallowest folder naming an equipment tag: the dossier the file is filed under."""
    file_name: str
    file_extension: str
    path_tags: tuple[str, ...]
    path_document_codes: tuple[str, ...]
    """Document codes in folder and file names, shallowest first."""
    document_class: str


@dataclass(frozen=True)
class UnitMetadata:
    route: str
    kind: str
    fields: tuple[FieldObservation, ...] = ()


def _unique(values: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return _unique(match.upper() for match in pattern.findall(text))


def _label(segment: str) -> str:
    return " ".join(_NUMBERED.sub("", segment).replace("_", " ").split())


def placement_metadata(blob_name: str) -> PlacementMetadata:
    segments = [segment for segment in blob_name.replace("\\", "/").split("/") if segment]
    file_name = segments[-1] if segments else blob_name
    folders = segments[:-1]
    zone = folders[0] if folders and not folders[0].startswith("_") else None
    dossier = next((index for index, folder in enumerate(folders) if TAG.search(folder)), None)
    equipment = folders[dossier] if dossier is not None else None
    stem, dot, extension = file_name.rpartition(".")
    return PlacementMetadata(
        zone_name=_label(zone) if zone else None,
        source_path=" / ".join(" ".join(part.replace("_", " ").split()) for part in segments),
        equipment_folder=_label(equipment) if equipment else None,
        file_name=file_name,
        file_extension=f".{extension.lower()}" if dot and stem else "",
        path_tags=tuple(_matches(TAG, "/".join(segments))),
        path_document_codes=tuple(_matches(DOCUMENT, "/".join(segments))),
        document_class=document_class(folders, dossier, file_name),
    )


def _chunk_fields(
    chunk: EvidenceChunk, units: Mapping[str, UnitMetadata]
) -> list[FieldObservation]:
    """Extracted (not generated) fields of the chunk's units that are on the chunk's pages."""

    pages = set(chunk.pages)
    found: list[FieldObservation] = []
    for unit_id in chunk.unit_ids:
        unit = units.get(unit_id)
        if unit is None:
            continue
        for item in unit.fields:
            if item.evidence_kind is not EvidenceKind.EXTRACTED_FIELD:
                continue
            if pages and item.pages and not pages & set(item.pages):
                continue
            found.append(item)
    return found


def _values(fields: Sequence[FieldObservation], *names: str) -> list[str]:
    return _unique(
        " ".join(str(item.value).split())
        for item in fields
        if item.field in names and isinstance(item.value, str | int | float)
    )


def _title_block(fields: Sequence[FieldObservation], tag: str) -> list[str]:
    return _unique(
        " ".join(str(item.value).split())
        for item in fields
        if item.field == "TitleBlock" and item.path.endswith(f".{tag}")
    )


def _codes(values: Sequence[str]) -> list[str]:
    """Whole printed numbers only: fragments such as ``PFDS 001 00`` or ``SP0904`` are dropped."""

    return _unique(code for code in (value.upper() for value in values) if _CODE.fullmatch(code))


def _revision(values: Sequence[str]) -> str | None:
    """A short revision mark; ``A0``-``A4`` is the sheet size printed beside it, not a revision."""

    return next(
        (v for v in values if _REVISION.fullmatch(v) and not _PAPER_SIZE.fullmatch(v)), None
    )


def _sheet(chunk: EvidenceChunk) -> str | None:
    """The cited sheet: a paper layout (the printed sheet) before model space, else the page."""

    sheets = [locator for locator in chunk.locators if locator.startswith("sheet:")]
    if sheets:
        return next((sheet for sheet in sheets if sheet != MODEL_SPACE), sheets[0])
    if chunk.kind in _DRAWING_KINDS and len(chunk.pages) == 1:
        return f"page:{chunk.pages[0]}"
    return None


def _where(chunk: EvidenceChunk, sheet: str | None) -> str | None:
    if sheet and sheet.startswith("sheet:"):
        return f"sheet {sheet.removeprefix('sheet:')}"
    if chunk.sheet:
        return f"sheet {chunk.sheet}"
    if len(chunk.pages) == 1:
        return f"p. {chunk.pages[0]}"
    if chunk.pages:
        return f"pp. {chunk.pages[0]}-{chunk.pages[-1]}"
    return None


def _title(printed: str | None, placement: PlacementMetadata, where: str | None) -> str:
    base = printed or placement.file_name.rsplit(".", 1)[0] or placement.file_name
    parts = [base]
    folder = placement.equipment_folder
    if folder and folder.casefold() not in base.casefold():
        parts.append(folder)
    if where:
        parts.append(where)
    title = " — ".join(parts)
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 1] + "…"


def row_metadata(
    chunk: EvidenceChunk,
    *,
    placements: Sequence[PlacementMetadata],
    units: Mapping[str, UnitMetadata],
    family: str,
) -> dict[str, object]:
    """``placements`` are every current location of the bytes, canonical first."""

    canonical = placements[0]
    path_tags = _unique(tag for placement in placements for tag in placement.path_tags)
    path_codes = _unique(code for placement in placements for code in placement.path_document_codes)
    fields = _chunk_fields(chunk, units)
    printed = _values(fields, "DocumentCode", "DrawingNumber")
    printed_codes = _codes(printed)
    if printed_codes:
        document_code, basis = printed_codes[0], "printed"
    elif canonical.path_document_codes:
        document_code, basis = canonical.path_document_codes[-1], "path"
    else:
        document_code, basis = None, None
    # A title-block revision is trusted only when its printed number was read whole.
    revision = _revision(_values(fields, "Revision") if printed_codes or not printed else [])
    revision = revision or _revision(_title_block(fields, "REVISION_INDEX"))
    issue_status = next(
        (value for value in _values(fields, "IssueStatus") if _ISSUE_STATUS.search(value)), None
    )
    mentioned = _unique(
        [value.upper() for value in _values(fields, "EquipmentTags")] + _matches(TAG, chunk.text)
    )
    identifiers = _unique(
        [
            *([document_code] if document_code else []),
            *printed_codes,
            *mentioned,
            *_matches(DOCUMENT, chunk.text),
            *[mark.upper() for mark in _title_block(fields, "POS_NUM")],
            *_values(fields, "StreamNumbers"),
            *path_tags,
            *path_codes,
        ]
    )[:MAX_IDENTIFIERS]
    primary = units.get(chunk.unit_ids[0]) if chunk.unit_ids else None
    sheet = _sheet(chunk)
    printed_titles = _values(fields, "Title")
    title = _title(printed_titles[0] if printed_titles else None, canonical, _where(chunk, sheet))
    language = chunk.language
    return {
        "title": title,
        "content_fr": chunk.text if language in (None, "fr") else None,
        "content_en": chunk.text if language in (None, "en") else None,
        "source_path": canonical.source_path,
        "source_paths": [placement.source_path for placement in placements],
        "equipment_folder": canonical.equipment_folder,
        "equipment_folders": _unique(placement.equipment_folder for placement in placements),
        "zone_name": canonical.zone_name,
        "zone_names": _unique(placement.zone_name for placement in placements),
        "document_class": canonical.document_class,
        "document_classes": _unique(placement.document_class for placement in placements),
        "file_extension": canonical.file_extension,
        "family": family,
        "unit_kind": primary.kind if primary else None,
        "extraction_route": primary.route if primary else None,
        "chunk_kind": chunk.kind.value,
        "identifiers": identifiers,
        "mentioned_tags": mentioned,
        "path_tags": path_tags,
        "equipment_tags": _unique([*mentioned, *path_tags]),
        "document_code": document_code,
        "document_code_basis": basis,
        "revision": revision,
        "issue_status": issue_status,
        "drawing_sheet_id": sheet,
        "process_case_id": chunk.process_case_id,
        "page_image_uri": None,
    }
