"""CRV search fields, keeping source text, placement context and derived lineage distinct."""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.chunks import EvidenceChunk
from ..domain.crv import REPORT_CODE, research_context, unique
from ..domain.models import SourceSnapshot


def research_fields(
    chunk: EvidenceChunk, placements: Sequence[SourceSnapshot]
) -> dict[str, object]:
    contexts = [
        research_context(s.provenance.source_path if s.provenance else s.blob_name)
        for s in placements
    ]
    primary = contexts[0]
    printed = unique([code.upper() for code in REPORT_CODE.findall(chunk.text)])
    path_codes = unique([code for context in contexts for code in context.report_codes])
    codes = printed or path_codes
    page = f" - p. {','.join(map(str, chunk.pages))}" if chunk.pages else ""
    return {
        "title": f"{primary.file_name}{page}",
        "source_path": primary.source_path,
        "source_paths": unique([c.source_path for c in contexts]),
        "collection": primary.collection,
        "collections": unique([c.collection for c in contexts]),
        "site_ids": unique([site for c in contexts for site in c.site_ids]),
        "document_type": primary.document_type,
        "document_class": primary.document_type,
        "document_classes": unique([c.document_type for c in contexts]),
        "metadata_basis": "path",
        "document_code": codes[0] if codes else None,
        "document_code_basis": "printed" if printed else "path" if codes else None,
        "identifiers": unique([*printed, *path_codes]),
        "equipment_folder": None,
        "equipment_folders": [],
        "equipment_tags": [],
        "path_tags": [],
        "mentioned_tags": [],
        "zone_name": None,
        "zone_names": [],
        "process_case_id": None,
        "revision": None,
        "issue_status": None,
        "original_source_uris": unique([
            f"{s.container_name}/{s.provenance.original_blob}"
            if s.provenance else f"{s.container_name}/{s.blob_name}"
            for s in placements
        ]),
        "original_content_hashes": unique([
            s.provenance.original_hash if s.provenance else s.content_hash for s in placements
        ]),
        "citation_basis": "derived_pdf" if any(
            s.provenance and s.provenance.converter for s in placements
        ) else "source",
    }
