from __future__ import annotations

from evidence_fakes import cu_content, cu_document, cu_source

from ingest_pipeline.adapters.parsers.tokenizers import ApproximateCounter
from ingest_pipeline.domain.chunks import ChunkKind
from ingest_pipeline.domain.evidence import (
    BlockKind,
    EvidenceBlock,
    EvidenceKind,
    EvidenceUnit,
    LocatorKind,
)
from ingest_pipeline.domain.models import ArtifactRef, UnitRecord
from ingest_pipeline.domain.routing import Route
from ingest_pipeline.stages.chunkers import DocumentChunker
from ingest_pipeline.stages.normalizers.cu import normalize_analysis

HASH = "c" * 64
RAW = ArtifactRef(sha256="e" * 64, uri="artifacts/cu.json", size_bytes=10, media_type="json")
METHODS = {"Tags": "extract", "Summary": "generate", "SheetType": "classify"}


def _unit(pages: tuple[int, ...] = (3, 4), kind: str = "drawing_sheet") -> UnitRecord:
    return UnitRecord(
        unit_id="unit:cu:00000",
        ordinal=0,
        kind=kind,
        route=Route.CU_ANALYZE.value,
        pages=pages,
        locator={"profile": "tizert_pfd_sheet_v1", "replaces": "di_layout_drawing"},
    )


def _normalize(document: dict, unit: UnitRecord | None = None) -> EvidenceUnit:
    return normalize_analysis(
        document,
        unit=unit or _unit(),
        plan_id="plan:cu",
        content_hash=HASH,
        raw=RAW,
        page_map=None,
        request={"analyzer_id": "tizert_pfd_sheet_v1", "content_range": "3-4"},
        profile={"analyzer_id": "tizert_pfd_sheet_v1"},
        field_methods=METHODS,
    )


def _fields(markdown: str) -> dict:
    offset = markdown.index("6140-ZM-2427")
    tag = {
        "type": "string",
        "valueString": "6140-ZM-2427",
        "spans": [{"offset": offset, "length": len("6140-ZM-2427")}],
        "source": cu_source(1),
        "confidence": 0.91,
    }
    return {
        "Tags": {"type": "array", "valueArray": [{"type": "object", "valueObject": {"Tag": tag}}]},
        "Summary": {"type": "string", "valueString": "Broyage et pompage du circuit 161."},
        "SheetType": {"type": "string", "valueString": "pfd"},
    }


def _document() -> dict:
    content = cu_content([1, 2])
    content["fields"] = _fields(content["markdown"])
    return cu_document(
        [content], analyzer_id="tizert_pfd_sheet_v1", pages=2,
        warnings=[{"code": "ContentFiltered", "message": "x"}],
    )


def test_cu_results_map_renumbered_pages_and_cite_regions_and_spans() -> None:
    unit = _normalize(_document())
    assert [page.page for page in unit.pages] == [3, 4]
    source = [block for block in unit.blocks if block.evidence_kind is EvidenceKind.SOURCE_TEXT]
    first = source[0]
    assert first.kind is BlockKind.HEADING
    assert first.pages == (3,)
    kinds = {locator.kind for locator in first.locators}
    assert kinds == {LocatorKind.REGION, LocatorKind.SPAN}
    span = next(loc for loc in first.locators if loc.kind is LocatorKind.SPAN)
    assert span.representation == "cu_markdown"
    assert span.index_type == "codePoint"
    assert span.artifact_sha256 == RAW.sha256
    assert unit.route == Route.CU_ANALYZE.value
    assert unit.provider["unit_kind"] == "drawing_sheet"
    assert unit.provider["replaces"] == "di_layout_drawing"
    assert unit.warnings == ("ContentFiltered",)
    assert len({block.evidence_id for block in unit.blocks}) == len(unit.blocks)


def test_figure_descriptions_follow_their_figure_as_generated_evidence() -> None:
    unit = _normalize(_document())
    figures = [block for block in unit.blocks if block.kind is BlockKind.FIGURE]
    assert [(block.figure.figure_id, block.evidence_kind) for block in figures if block.figure] == [
        ("1.1", EvidenceKind.SOURCE_TEXT),
        ("1.1", EvidenceKind.GENERATED_DESCRIPTION),
        ("2.1", EvidenceKind.SOURCE_TEXT),
        ("2.1", EvidenceKind.GENERATED_DESCRIPTION),
    ]
    generated = figures[1]
    assert generated.text == "Schema de procede de la page 1."
    assert generated.pages == (3,)


def test_fields_keep_the_printed_text_method_and_location() -> None:
    unit = _normalize(_document())
    by_path = {field.path: field for field in unit.fields}
    tag = by_path["Tags[0].Tag"]
    assert tag.value == "6140-ZM-2427"
    assert tag.raw_text == "6140-ZM-2427"
    assert tag.evidence_kind is EvidenceKind.EXTRACTED_FIELD
    assert tag.pages == (3,)
    assert tag.confidence == 0.91
    assert {locator.kind for locator in tag.locators} == {LocatorKind.REGION, LocatorKind.SPAN}
    assert by_path["SheetType"].evidence_kind is EvidenceKind.CLASSIFIED_LABEL
    assert by_path["Summary"].evidence_kind is EvidenceKind.GENERATED_DESCRIPTION
    assert by_path["Summary"].raw_text is None
    summary = [block for block in unit.blocks if block.role == "field:Summary"]
    assert [block.evidence_kind for block in summary] == [EvidenceKind.GENERATED_DESCRIPTION]
    assert summary[0] is unit.blocks[-1]


def test_unknown_methods_are_treated_as_generated() -> None:
    content = cu_content([1], descriptions=False)
    content["fields"] = {"Other": {"type": "string", "valueString": "texte libre"}}
    unit = normalize_analysis(
        cu_document([content]),
        unit=_unit((1,), kind="pdf_pages"),
        plan_id="plan:cu",
        content_hash=HASH,
        raw=RAW,
        page_map=None,
        request={"analyzer_id": "tizert_doc_fields_v1"},
        profile={},
        field_methods={},
    )
    assert unit.fields[0].evidence_kind is EvidenceKind.GENERATED_DESCRIPTION
    assert all(block.evidence_kind is EvidenceKind.SOURCE_TEXT for block in unit.blocks[:-1])
    assert unit.blocks[-1].role == "field:Other"


def test_generated_text_is_chunked_alone_and_drawing_kind_is_kept() -> None:
    chunker = DocumentChunker(evidence_id="evd:cu", tokens=ApproximateCounter())
    chunker.feed(_normalize(_document()))
    chunks = chunker.finish()
    generated = [chunk for chunk in chunks if chunk.evidence_kind is not EvidenceKind.SOURCE_TEXT]
    source = [chunk for chunk in chunks if chunk.evidence_kind is EvidenceKind.SOURCE_TEXT]
    assert generated
    assert source
    assert all("Schema de procede" not in chunk.text for chunk in source)
    assert all("circuit 161" not in chunk.text for chunk in source)
    assert {chunk.kind for chunk in source} <= {ChunkKind.DRAWING_TEXT, ChunkKind.FIGURE}
    assert any(chunk.kind is ChunkKind.DRAWING_TEXT for chunk in source)


def test_flow_units_never_merge_generated_paragraphs_into_prose() -> None:
    blocks = (
        EvidenceBlock(evidence_id="a", kind=BlockKind.PARAGRAPH, text="Graisser les paliers."),
        EvidenceBlock(
            evidence_id="b",
            kind=BlockKind.PARAGRAPH,
            text="Resume genere.",
            evidence_kind=EvidenceKind.GENERATED_DESCRIPTION,
        ),
        EvidenceBlock(evidence_id="c", kind=BlockKind.PARAGRAPH, text="Verifier la pompe."),
    )
    unit = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route=Route.CU_ANALYZE.value,
        extraction_method="cu_analyze", normalizer="test", blocks=blocks,
    )
    chunker = DocumentChunker(evidence_id="evd:f", tokens=ApproximateCounter())
    chunker.feed(unit)
    chunks = chunker.finish()
    assert [(chunk.evidence_kind, chunk.text) for chunk in chunks if "genere" in chunk.text] == [
        (EvidenceKind.GENERATED_DESCRIPTION, "Resume genere.")
    ]
    source = "\n".join(c.text for c in chunks if c.evidence_kind is EvidenceKind.SOURCE_TEXT)
    assert "Graisser" in source
    assert "Verifier" in source
