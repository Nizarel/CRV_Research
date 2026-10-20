"""Choose a deterministic, stratified pilot sample from the local corpus and predict its cost.

Pages are profiled with the production PDFium adapter and planned with the production routing
policy, so the prediction counts exactly the pages the policy sends to Document Intelligence.
Legacy Office files are counted from their originals' converted page counts when available;
otherwise they are listed as unknown so their cost is never silently assumed to be zero.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.crv import research_context
from ..domain.errors import UnsupportedContentError
from ..domain.routing import (
    DI_ROUTES,
    FAMILY_BY_KIND,
    Family,
    PreflightReport,
    RoutingPolicy,
    plan_units,
)
from ..domain.sniff import SNIFF_BYTES, FileKind, Sniffed, sniff
from .local_intake import long_path, reparse, safe_member
from .parsers.pdfium import PdfiumToolkit

MEMBER_SEPARATOR = "!/"


@dataclass(frozen=True)
class Candidate:
    entry: str
    """Corpus-relative path; ``archive.zip!/member`` for a ZIP member."""
    kind: str
    stratum: str
    collection: str
    size_bytes: int
    di_pages: int | None
    """Pages routed to Document Intelligence; ``None`` when unknown before conversion."""
    total_pages: int | None = None
    notes: tuple[str, ...] = ()


@dataclass
class Selection:
    entries: list[Candidate] = field(default_factory=list)

    @property
    def di_pages(self) -> int:
        return sum(item.di_pages or 0 for item in self.entries)

    @property
    def unknown(self) -> list[str]:
        return [item.entry for item in self.entries if item.di_pages is None]


def _stratum(entry: str, kind: FileKind, report: PreflightReport) -> str:
    name = entry.casefold()
    if kind is FileKind.CFB_OTHER:
        return "legacy_office"
    if report.mismatch:
        return "mislabeled"
    if kind in (FileKind.XLSX, FileKind.XLSB, FileKind.XLS, FileKind.ODS):
        return "spreadsheet"
    context = research_context(entry)
    if context.collection == "lims-commercial":
        return "lims_commercial"
    if context.document_type in ("template", "procedure", "regulation"):
        return "procedure_template"
    if not report.pages:
        return "other"
    pages = report.pages
    scanned = sum(1 for page in pages if page.text_chars < 80 and page.image_objects)
    columns = sum(1 for page in pages if page.multi_column)
    if scanned * 2 >= len(pages):
        return "scanned_report"
    if columns * 4 >= len(pages) or "et al" in name:
        return "two_column_paper"
    return f"{context.document_type}"


def _report(
    entry: str, data_path: Path, sniffed: Sniffed, size: int, pages: Any
) -> PreflightReport:
    return PreflightReport(
        content_hash=hashlib.sha256(entry.encode()).hexdigest(),
        size_bytes=size,
        snapshot_id="pilot",
        blob_name=entry,
        extension=sniffed.extension,
        file_kind=sniffed.kind,
        declared_kind=sniffed.declared,
        family=FAMILY_BY_KIND.get(sniffed.kind, Family.UNKNOWN),
        page_count=pages.page_count if pages else None,
        pages=pages.pages if pages else (),
    )


def _di_pages(report: PreflightReport, policy: RoutingPolicy) -> int:
    return sum(
        max(len(unit.pages), 1)
        for unit in plan_units(report, policy)
        if unit.extractable and unit.route in DI_ROUTES
    )


async def profile_corpus(
    root: Path, policy: RoutingPolicy, *, toolkit: PdfiumToolkit | None = None
) -> list[Candidate]:
    """Predict DI pages for every file (and every ZIP member) under ``root``."""

    toolkit = toolkit or PdfiumToolkit()
    root = long_path(root)
    candidates: list[Candidate] = []
    for path in _walk(root):
        entry = path.relative_to(root).as_posix()
        with path.open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
        if _is_archive(path, entry, head):
            candidates.extend(await _archive(path, entry, policy, toolkit))
            continue
        candidates.append(await _candidate(path, entry, head, policy, toolkit))
    return candidates


def _is_archive(path: Path, entry: str, head: bytes) -> bool:
    """A plain ZIP, decided like intake: OOXML/ODF containers are documents, not archives."""

    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
    except zipfile.BadZipFile:
        return False
    return sniff(head, name=entry, zip_entries=names).kind is FileKind.ZIP


def _walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not reparse(Path(folder) / d))
        found.extend(
            Path(folder) / name for name in sorted(files) if not reparse(Path(folder) / name)
        )
    return found


async def _candidate(
    path: Path, entry: str, head: bytes, policy: RoutingPolicy, toolkit: PdfiumToolkit
) -> Candidate:
    entries = None
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as bundle:
                entries = bundle.namelist()
        except zipfile.BadZipFile:
            entries = None
    sniffed = sniff(head, name=entry, zip_entries=entries)
    kind = sniffed.kind
    size = (await asyncio.to_thread(path.stat)).st_size
    profile = None
    notes: list[str] = []
    if kind is FileKind.PDF:
        try:
            profile = await toolkit.profile(path)
        except UnsupportedContentError as error:
            notes.append(str(error))
    report = _report(entry, path, sniffed, size, profile)
    di: int | None = _di_pages(report, policy)
    if kind is FileKind.CFB_OTHER:
        di = None
    return Candidate(
        entry=entry, kind=kind.value, stratum=_stratum(entry, kind, report),
        collection=research_context(entry).collection, size_bytes=size, di_pages=di,
        total_pages=profile.page_count if profile else None, notes=tuple(notes),
    )


async def _archive(
    path: Path, entry: str, policy: RoutingPolicy, toolkit: PdfiumToolkit
) -> list[Candidate]:
    found: list[Candidate] = []
    with zipfile.ZipFile(path) as bundle, tempfile.TemporaryDirectory() as folder:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            member = safe_member(info)
            local = Path(folder) / "member"
            with bundle.open(info) as stream, local.open("wb") as copy:
                while chunk := stream.read(1024 * 1024):
                    copy.write(chunk)
            with local.open("rb") as handle:
                head = handle.read(SNIFF_BYTES)
            candidate = await _candidate(
                local, f"{entry}{MEMBER_SEPARATOR}{member}", head, policy, toolkit
            )
            found.append(candidate)
    return found


REQUIRED = (
    "scanned_report", "two_column_paper", "legacy_office", "mislabeled",
    "lims_commercial", "procedure_template", "spreadsheet",
)


def select(
    candidates: Sequence[Candidate], *, target_pages: int, seed: int,
    must_include: Iterable[str] = (), max_document_pages: int = 120,
) -> Selection:
    """Required entries and one per required stratum first, then a seeded fill by stratum.

    Documents larger than ``max_document_pages`` predicted DI pages are skipped by the fill so
    one long report cannot consume the pilot; explicitly required entries are always kept.
    """

    rng = random.Random(seed)
    by_entry = {item.entry: item for item in candidates}
    chosen: dict[str, Candidate] = {}
    for entry in must_include:
        if entry not in by_entry:
            raise ValueError(f"Required pilot entry not found: {entry}")
        chosen[entry] = by_entry[entry]

    def pages() -> int:
        return sum(item.di_pages or 0 for item in chosen.values())

    pools: dict[str, list[Candidate]] = {}
    for item in candidates:
        if item.entry in chosen or item.kind in ("text", "zip", "unknown"):
            continue
        if item.di_pages is not None and item.di_pages > max_document_pages:
            continue
        pools.setdefault(item.stratum, []).append(item)
    for pool in pools.values():
        pool.sort(key=lambda item: item.entry)
        rng.shuffle(pool)
    for stratum in REQUIRED:
        if any(item.stratum == stratum for item in chosen.values()):
            continue
        if pools.get(stratum):
            item = pools[stratum].pop()
            chosen[item.entry] = item
    order = sorted(pools)
    while pages() < target_pages and any(pools.values()):
        for stratum in order:
            if pools[stratum] and pages() < target_pages:
                item = pools[stratum].pop()
                if pages() + (item.di_pages or 0) <= target_pages * 1.1:
                    chosen[item.entry] = item
    return Selection(sorted(chosen.values(), key=lambda item: item.entry))


def selection_report(
    selection: Selection, *, di_page_usd: float, cu_page_usd: float
) -> dict[str, Any]:
    strata: dict[str, dict[str, int]] = {}
    for item in selection.entries:
        bucket = strata.setdefault(item.stratum, {"files": 0, "di_pages": 0})
        bucket["files"] += 1
        bucket["di_pages"] += item.di_pages or 0
    pages = selection.di_pages
    return {
        "files": len(selection.entries),
        "predicted_di_pages": pages,
        "unknown_pages": selection.unknown,
        "predicted_usd": {
            "di_layout": round(pages * di_page_usd, 2),
            "cu_extraction": round(pages * cu_page_usd, 2),
        },
        "strata": strata,
        "entries": [
            {
                "entry": item.entry, "stratum": item.stratum, "kind": item.kind,
                "collection": item.collection, "di_pages": item.di_pages,
                "total_pages": item.total_pages, "bytes": item.size_bytes,
                **({"notes": list(item.notes)} if item.notes else {}),
            }
            for item in selection.entries
        ],
    }
