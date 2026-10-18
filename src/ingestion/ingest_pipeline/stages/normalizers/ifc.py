"""Identity summary of an IFC (STEP-21) model: names, tags and document numbers, never geometry.

The file is streamed one record at a time, so models of hundreds of megabytes stay small in
memory. Only header, spatial, element, material and identifier-bearing records are parsed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

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
from .native import _unit

NATIVE_IFC = "native-ifc@1"
MAX_IDENTIFIERS = 500
MAX_ELEMENT_ROWS = 40
MAX_NAMED_ELEMENTS = 200

TAG = re.compile(r"(?<![A-Z0-9])\d{4}-[A-Z]{1,4}-\d{3,5}[A-Z]?(?![A-Z0-9])", re.IGNORECASE)
DOCUMENT = re.compile(
    r"(?<![A-Z0-9])SP0904-[0-9A-Z.]{2,}(?:-[0-9A-Z.]+)*-[0-9A-Z]{3,}(?![0-9A-Z])", re.IGNORECASE
)
_RECORD_HEAD = re.compile(r"\s*#\d+\s*=\s*([A-Z0-9_]+)\s*\(")
_ESCAPE = re.compile(r"\\X2\\((?:[0-9A-F]{4})+)\\X0\\|\\X\\([0-9A-F]{2})|\\S\\(.)", re.IGNORECASE)

PROJECT = "IFCPROJECT"
SPATIAL_LONG_NAME = {PROJECT: 5, "IFCSITE": 7, "IFCBUILDING": 7, "IFCBUILDINGSTOREY": 7}
ELEMENT_TYPES = frozenset(
    {
        "IFCBEAM", "IFCCOLUMN", "IFCMEMBER", "IFCPLATE", "IFCSLAB", "IFCWALL",
        "IFCWALLSTANDARDCASE", "IFCSTAIR", "IFCSTAIRFLIGHT", "IFCRAILING", "IFCRAMP",
        "IFCRAMPFLIGHT", "IFCROOF", "IFCFOOTING", "IFCPILE", "IFCDOOR", "IFCWINDOW",
        "IFCCOVERING", "IFCCURTAINWALL", "IFCBUILDINGELEMENTPROXY", "IFCBUILDINGELEMENTPART",
        "IFCDISCRETEACCESSORY", "IFCFASTENER", "IFCMECHANICALFASTENER", "IFCREINFORCINGBAR",
        "IFCREINFORCINGMESH", "IFCTENDON", "IFCELEMENTASSEMBLY", "IFCFLOWTERMINAL",
        "IFCFLOWSEGMENT", "IFCFLOWFITTING", "IFCFLOWCONTROLLER", "IFCFLOWMOVINGDEVICE",
        "IFCFLOWSTORAGEDEVICE", "IFCFLOWTREATMENTDEVICE", "IFCENERGYCONVERSIONDEVICE",
        "IFCDISTRIBUTIONCONTROLELEMENT", "IFCDISTRIBUTIONELEMENT", "IFCDISTRIBUTIONFLOWELEMENT",
        "IFCFURNISHINGELEMENT", "IFCTRANSPORTELEMENT", "IFCEQUIPMENTELEMENT",
        "IFCELECTRICALELEMENT", "IFCVIRTUALELEMENT",
    }
)
"""IFC2X3 ``IfcElement`` subtypes: Name is attribute 2, ObjectType 4 and Tag 7."""


@dataclass
class Spatial:
    entity: str
    name: str | None
    long_name: str | None
    description: str | None
    line: int


@dataclass
class Identifier:
    value: str
    kind: str
    """``tag`` or ``document``."""
    line: int
    where: str
    """Entity (and property name) of the first occurrence."""
    count: int = 1


@dataclass
class IfcSummary:
    file_name: str | None = None
    timestamp: str | None = None
    organization: str | None = None
    originating_system: str | None = None
    schema: str | None = None
    header_lines: int = 0
    lines: int = 0
    spatial: list[Spatial] = field(default_factory=list)
    element_counts: Counter[str] = field(default_factory=Counter)
    element_names: dict[str, Counter[str]] = field(default_factory=dict)
    named_elements: list[tuple[str, str, int]] = field(default_factory=list)
    """``(entity, text, line)`` for elements whose Name, ObjectType or Tag holds an identifier."""
    materials: Counter[str] = field(default_factory=Counter)
    identifiers: dict[str, Identifier] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def decode_string(raw: str) -> str:
    """STEP-21 string body (between the quotes) to text."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            return bytes.fromhex(match.group(1)).decode("utf-16-be", errors="replace")
        if match.group(2):
            return bytes.fromhex(match.group(2)).decode("latin-1")
        return chr(ord(match.group(3)) + 128)

    return _ESCAPE.sub(replace, raw.replace("''", "'")).replace("\\\\", "\\")


def split_arguments(body: str) -> list[str]:
    """Top-level arguments of a record body, respecting strings and nested lists."""

    arguments: list[str] = []
    depth = 0
    quoted = False
    start = 0
    for index, char in enumerate(body):
        if char == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append(body[start:index].strip())
            start = index + 1
    arguments.append(body[start:].strip())
    return arguments


def string_value(argument: str | None) -> str | None:
    if argument is None or len(argument) < 2 or argument[0] != "'" or argument[-1] != "'":
        return None
    text = decode_string(argument[1:-1]).strip()
    return text or None


def _argument(arguments: list[str], index: int) -> str | None:
    return arguments[index] if index < len(arguments) else None


def _body(record: str) -> str:
    return record[record.index("(") + 1 : record.rindex(")")]


def _records(handle: TextIO, summary: IfcSummary) -> Iterator[tuple[int, int, str]]:
    """``(first_line, last_line, record)`` for every DATA record; fills the header fields."""

    header: list[str] = []
    in_data = False
    buffer: list[str] = []
    quotes = 0
    start = 0
    number = 0
    for number, line in enumerate(handle, start=1):
        if not in_data:
            if line.strip().upper() == "DATA;":
                in_data = True
                summary.header_lines = number
                _read_header("".join(header), summary)
            else:
                header.append(line)
                if "'" in line:
                    strings = re.findall(r"'((?:[^']|'')*)'", line)
                    text = " ".join(decode_string(value) for value in strings)
                    _note_identifiers(summary, text, number, "FILE header")
            continue
        if not buffer:
            if not line.lstrip().startswith("#"):
                continue
            start = number
        buffer.append(line)
        quotes += line.count("'")
        # '' escapes keep the count even, so an odd count means an open string.
        if quotes % 2 == 0 and line.rstrip().endswith(";"):
            yield start, number, "".join(buffer)
            buffer, quotes = [], 0
    summary.lines = number
    if not in_data:
        summary.warnings.append("no DATA section")


def _read_header(text: str, summary: IfcSummary) -> None:
    name = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", text, re.DOTALL)
    if name:
        arguments = split_arguments(name.group(1))
        summary.file_name = string_value(_argument(arguments, 0))
        summary.timestamp = string_value(_argument(arguments, 1))
        organization = _argument(arguments, 3)
        if organization and organization.startswith("("):
            organization = split_arguments(organization[1:-1])[0]
        summary.organization = string_value(organization)
        summary.originating_system = string_value(_argument(arguments, 5))
    schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", text)
    if schema:
        summary.schema = schema.group(1)


def _note_identifiers(summary: IfcSummary, text: str, line: int, where: str) -> None:
    for kind, pattern in (("document", DOCUMENT), ("tag", TAG)):
        for match in pattern.findall(text):
            value = match.upper()
            known = summary.identifiers.get(value)
            if known is not None:
                known.count += 1
            elif len(summary.identifiers) < MAX_IDENTIFIERS:
                summary.identifiers[value] = Identifier(value, kind, line, where)
            elif "identifiers truncated" not in summary.warnings:
                summary.warnings.append("identifiers truncated")


def read_ifc(path: Path) -> IfcSummary:
    summary = IfcSummary()
    with path.open("r", encoding="latin-1", newline=None) as handle:
        for first, _, record in _records(handle, summary):
            head = _RECORD_HEAD.match(record)
            if head is None:
                continue
            entity = head.group(1).upper()
            if entity in SPATIAL_LONG_NAME:
                arguments = split_arguments(_body(record))
                item = Spatial(
                    entity=entity,
                    name=string_value(_argument(arguments, 2)),
                    long_name=string_value(_argument(arguments, SPATIAL_LONG_NAME[entity])),
                    description=string_value(_argument(arguments, 3)),
                    line=first,
                )
                summary.spatial.append(item)
                for text in (item.name, item.long_name, item.description):
                    if text:
                        _note_identifiers(summary, text, first, entity)
            elif entity in ELEMENT_TYPES:
                arguments = split_arguments(_body(record))
                summary.element_counts[entity] += 1
                name = string_value(_argument(arguments, 2))
                if name:
                    summary.element_names.setdefault(entity, Counter())[name] += 1
                texts = [
                    text
                    for text in (
                        name,
                        string_value(_argument(arguments, 4)),
                        string_value(_argument(arguments, 7)),
                    )
                    if text
                ]
                joined = " | ".join(texts)
                if joined and (TAG.search(joined) or DOCUMENT.search(joined)):
                    _note_identifiers(summary, joined, first, entity)
                    if len(summary.named_elements) < MAX_NAMED_ELEMENTS:
                        summary.named_elements.append((entity, joined, first))
            elif entity == "IFCMATERIAL":
                material = string_value(_argument(split_arguments(_body(record)), 0))
                if material:
                    summary.materials[material] += 1
            elif "'" in record and (TAG.search(record) or DOCUMENT.search(record)):
                where = entity
                if entity == "IFCPROPERTYSINGLEVALUE":
                    property_name = string_value(_argument(split_arguments(_body(record)), 0))
                    where = f"{entity} {property_name}" if property_name else entity
                strings = re.findall(r"'((?:[^']|'')*)'", record)
                text = " ".join(decode_string(value) for value in strings)
                _note_identifiers(summary, text, first, where)
    return summary


def _lines(line: int) -> Locator:
    return Locator(kind=LocatorKind.LINES, line_start=line, line_end=line)


def normalize_ifc(
    summary: IfcSummary, *, unit: UnitRecord, plan_id: str, content_hash: str
) -> EvidenceUnit:
    blocks: list[EvidenceBlock] = []
    origin = "; ".join(
        part
        for part in (
            f"file {summary.file_name}" if summary.file_name else None,
            f"schema {summary.schema}" if summary.schema else None,
            f"exported by {summary.originating_system}" if summary.originating_system else None,
            f"organization {summary.organization}" if summary.organization else None,
            f"on {summary.timestamp}" if summary.timestamp else None,
        )
        if part
    )
    header_lines = max(summary.header_lines, 1)
    blocks.append(
        EvidenceBlock(
            evidence_id="pending",
            kind=BlockKind.TITLE,
            text=f"IFC model: {origin}" if origin else "IFC model",
            level=1,
            locators=(Locator(kind=LocatorKind.LINES, line_start=1, line_end=header_lines),),
        )
    )
    for item in summary.spatial:
        parts = [f"{item.entity} name {item.name}" if item.name else item.entity]
        if item.long_name:
            parts.append(f"long name {item.long_name}")
        if item.description:
            parts.append(f"description {item.description}")
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.PARAGRAPH,
                text="; ".join(parts),
                locators=(_lines(item.line),),
            )
        )
    for kind, label in (("tag", "Equipment tags"), ("document", "Document numbers")):
        found = [ident for ident in summary.identifiers.values() if ident.kind == kind]
        if found:
            blocks.append(
                EvidenceBlock(
                    evidence_id="pending",
                    kind=BlockKind.PARAGRAPH,
                    text=f"{label} in the model: " + ", ".join(ident.value for ident in found),
                    locators=tuple(_lines(ident.line) for ident in found),
                )
            )
    for entity, text, line in summary.named_elements:
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.PARAGRAPH,
                text=f"{entity}: {text}",
                locators=(_lines(line),),
            )
        )
    if summary.element_counts:
        rows = summary.element_counts.most_common(MAX_ELEMENT_ROWS)
        cells = [
            EvidenceCell(row=0, column=column, kind=CellKind.COLUMN_HEADER, text=text)
            for column, text in enumerate(("IFC type", "Count", "Most frequent names"))
        ]
        for row, (entity, count) in enumerate(rows, start=1):
            names = summary.element_names.get(entity, Counter()).most_common(5)
            values = (entity, str(count), ", ".join(name for name, _ in names))
            cells.extend(
                EvidenceCell(row=row, column=column, text=text)
                for column, text in enumerate(values)
            )
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.TABLE,
                locators=(Locator(kind=LocatorKind.DOCUMENT),),
                table=EvidenceTable(
                    row_count=len(rows) + 1,
                    column_count=3,
                    cells=tuple(cells),
                    header_rows=1,
                    caption="Element inventory",
                ),
            )
        )
    if summary.materials:
        common = summary.materials.most_common(30)
        materials = ", ".join(f"{name} ({count})" for name, count in common)
        blocks.append(
            EvidenceBlock(
                evidence_id="pending",
                kind=BlockKind.PARAGRAPH,
                text=f"Materials: {materials}",
                locators=(Locator(kind=LocatorKind.DOCUMENT),),
            )
        )
    digest = sha256_hex(
        "|".join(f"{ident.value}@{ident.line}" for ident in summary.identifiers.values())
        + f"|{summary.lines}|{sorted(summary.element_counts.items())}"
    )
    evidence = _unit(
        unit,
        plan_id=plan_id,
        content_hash=content_hash,
        method="native_ifc",
        normalizer=NATIVE_IFC,
        source_digest=digest,
        pages=(),
        blocks=blocks,
        provider={"engine": "step21-identity", "schema": summary.schema, "lines": summary.lines},
        warnings=summary.warnings,
    )
    return evidence.model_copy(update={"fields": tuple(_fields(summary))})


def _fields(summary: IfcSummary) -> Iterator[FieldObservation]:
    counters = {"tag": 0, "document": 0}
    names = {"tag": "EquipmentTags", "document": "DocumentCode"}
    for ident in summary.identifiers.values():
        index = counters[ident.kind]
        counters[ident.kind] += 1
        yield FieldObservation(
            path=f"{names[ident.kind]}[{index}]",
            field=names[ident.kind],
            method="native",
            evidence_kind=EvidenceKind.EXTRACTED_FIELD,
            value_type="string",
            value=ident.value,
            raw_text=ident.value,
            locators=(_lines(ident.line),),
            analyzer_id=NATIVE_IFC,
        )
