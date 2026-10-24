"""KG-1 inputs from what ingestion recorded: evidence documents of one routing policy, every
source placement of their content, page-header codes, and page-1/2 blocks (cover lines).

Reads PostgreSQL (``ingest.evidence_document``, ``ingest.source_snapshot``) and the evidence
artifacts; writes nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Collection, Sequence
from typing import Any

from ...domain.crv_kg.build import DocumentInput, Placement
from ...domain.crv_kg.cover import CoverLine
from ...domain.crv_kg.study import StudyDocument, study_pages
from ...domain.crv_kg.table_rules import table_observation
from ...domain.evidence import BlockKind, EvidenceDocument, EvidenceUnit
from ...domain.models import ArtifactRef
from ...domain.observations import numeric_observations
from ...ports.storage import ArtifactStore
from .pool import PgPool

COVER_PAGES = (1, 2)
_TEXT_KINDS = frozenset({BlockKind.TITLE, BlockKind.HEADING, BlockKind.PARAGRAPH,
                         BlockKind.PAGE_HEADER})
_DOCUMENTS = """
SELECT DISTINCT ON (content_hash) evidence_id, content_hash, manifest
FROM ingest.evidence_document
WHERE policy_version = $1 AND status = ANY ($2::text[])
ORDER BY content_hash, (status = 'embedded') DESC, updated_at DESC, evidence_id
"""
_PLACEMENTS = """
SELECT DISTINCT ON (content_hash, coalesce(provenance ->> 'source_path', blob_name))
    content_hash, blob_name, provenance
FROM ingest.source_snapshot
WHERE content_hash = ANY ($1::text[])
ORDER BY content_hash, coalesce(provenance ->> 'source_path', blob_name), observed_at DESC
"""


def cover_lines(units: Sequence[EvidenceUnit]) -> tuple[CoverLine, ...]:
    """Lines of pages 1-2 in reading order: paragraph lines, then table rows as joined cells."""

    return page_lines(units, COVER_PAGES)


def page_lines(units: Sequence[EvidenceUnit], pages: Collection[int]) -> tuple[CoverLine, ...]:
    """Lines of the given pages in reading order: text lines, then table rows as joined cells."""

    lines: list[CoverLine] = []
    for unit in units:
        for block in unit.blocks:
            page = min(block.pages) if block.pages else None
            if page is None or page not in pages:
                continue
            if block.kind in _TEXT_KINDS:
                lines.extend(
                    CoverLine(text.strip(), page, block.evidence_id)
                    for text in block.text.splitlines() if text.strip()
                )
            elif block.kind is BlockKind.TABLE and block.table is not None:
                for row in block.table.grid():
                    cells = [cell.strip() for cell in dict.fromkeys(row) if cell.strip()]
                    if cells:
                        lines.append(CoverLine(" ".join(cells), page, block.evidence_id))
    return tuple(lines)


def _placement(row: Any) -> Placement:
    provenance = row["provenance"] or {}
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    return Placement(
        source_path=str(provenance.get("source_path") or row["blob_name"]),
        original_hash=provenance.get("original_hash"),
        converter=provenance.get("converter"),
        archive_members=tuple(provenance.get("archive_members") or ()),
    )


async def load_inputs(
    pool: PgPool,
    artifacts: ArtifactStore,
    *,
    policy_version: str,
    statuses: tuple[str, ...] = ("embedded",),
    concurrency: int = 16,
) -> tuple[list[DocumentInput], dict[str, int]]:
    """Documents with their placements and cover lines, plus load counters."""

    async with pool.acquire() as connection:
        rows = await connection.fetch(_DOCUMENTS, policy_version, list(statuses))
        hashes = [row["content_hash"] for row in rows]
        placements: dict[str, list[Placement]] = defaultdict(list)
        for row in await connection.fetch(_PLACEMENTS, hashes):
            placements[row["content_hash"]].append(_placement(row))
    gate = asyncio.Semaphore(concurrency)
    counters: dict[str, int] = defaultdict(int)

    async def one(row: Any) -> DocumentInput | None:
        if not placements.get(row["content_hash"]):
            counters["without_placement"] += 1
            return None
        async with gate:
            manifest_ref = ArtifactRef.model_validate(row["manifest"])
            manifest = EvidenceDocument.model_validate_json(await artifacts.get(manifest_ref))
            units: list[EvidenceUnit] = []
            for summary in manifest.units:
                if summary.evidence is None:
                    continue
                if summary.pages and not set(summary.pages) & set(COVER_PAGES):
                    continue
                units.append(
                    EvidenceUnit.model_validate_json(await artifacts.get(summary.evidence))
                )
        counters["documents"] += 1
        counters["header_codes"] += bool(manifest.document_codes)
        return DocumentInput(
            content_hash=row["content_hash"],
            evidence_id=row["evidence_id"],
            page_count=manifest.page_count,
            placements=tuple(sorted(placements[row["content_hash"]],
                                    key=lambda item: item.source_path)),
            header_codes=manifest.document_codes,
            cover_lines=cover_lines(units),
        )

    loaded = await asyncio.gather(*(one(row) for row in rows))
    counters["evidence_documents"] = len(rows)
    return [item for item in loaded if item is not None], dict(counters)


async def load_study_inputs(
    pool: PgPool,
    artifacts: ArtifactStore,
    *,
    policy_version: str,
    folder: str,
    statuses: tuple[str, ...] = ("embedded",),
    concurrency: int = 16,
    tables: bool = False,
    only: Collection[str] | None = None,
) -> list[StudyDocument]:
    """KG-2 inputs: documents placed under ``folder`` (case-insensitive path segment), or only
    those of ``only`` content hashes among them, with the text of their ``study_pages``; with
    ``tables``, every unit is read for typed table cells."""

    segment = folder.casefold()
    async with pool.acquire() as connection:
        rows = await connection.fetch(_DOCUMENTS, policy_version, list(statuses))
        paths: dict[str, list[str]] = defaultdict(list)
        for row in await connection.fetch(_PLACEMENTS, [r["content_hash"] for r in rows]):
            paths[row["content_hash"]].append(_placement(row).source_path)
    selected = [
        row for row in rows
        if (only is None or row["content_hash"] in only)
        and any(segment in part.casefold() for path in paths[row["content_hash"]]
                for part in path.split("/")[:-1])
    ]
    gate = asyncio.Semaphore(concurrency)

    async def one(row: Any) -> StudyDocument:
        async with gate:
            manifest = EvidenceDocument.model_validate_json(
                await artifacts.get(ArtifactRef.model_validate(row["manifest"]))
            )
            wanted = study_pages(manifest.page_count or 0)
            units = [
                EvidenceUnit.model_validate_json(await artifacts.get(summary.evidence))
                for summary in manifest.units
                if summary.evidence is not None
                and (tables or not summary.pages or set(summary.pages) & set(wanted))
            ]
        text: dict[int, list[str]] = defaultdict(list)
        for line in page_lines(units, wanted):
            text[line.page].append(line.text)
        cells = [
            (page, typed)
            for unit in units if tables
            for cell in numeric_observations(unit)
            if (page := next((loc.page for loc in cell.locators if loc.page), None)) is not None
            and (typed := table_observation(cell)) is not None
        ]
        return StudyDocument(
            content_hash=row["content_hash"],
            source_path=sorted(paths[row["content_hash"]])[0],
            page_count=manifest.page_count or 0,
            pages={page: "\n".join(lines) for page, lines in sorted(text.items())},
            tables=tuple(cells),
        )

    return list(await asyncio.gather(*(one(row) for row in selected)))
