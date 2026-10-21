from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fixture_builders import PdfPage, pdf_bytes

from ingest_pipeline.adapters.local_intake import CorpusIntake, collect_legacy_office
from ingest_pipeline.adapters.parsers.office import PrecomputedOfficeConverter
from ingest_pipeline.adapters.parsers.tokenizers import ApproximateCounter
from ingest_pipeline.domain.chunks import ChunkKind, EvidenceChunk
from ingest_pipeline.domain.crv import report_codes
from ingest_pipeline.domain.evidence import (
    BlockKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceTable,
    EvidenceUnit,
)
from ingest_pipeline.domain.furniture import FurnitureScan
from ingest_pipeline.domain.intake import IntakeLimits
from ingest_pipeline.domain.models import SourceSnapshot
from ingest_pipeline.stages.chunkers import DocumentChunker
from ingest_pipeline.stages.research_fields import research_fields

IMAGE = "sha256:" + "c" * 64
HEADER = "Geochemical Assessment and Waste Charactisation Tizert Project"


def test_report_codes_repair_ocr_slash_and_keep_dotted_parts() -> None:
    assert report_codes("| Reminex | CRIAR/PE/RP/09/25 |") == ["CR/AR/PE/RP/09/25"]
    assert report_codes("cr/ge/rp/94/20 and CR/RC/P/01.00.") == ["CR/GE/RP/94/20", "CR/RC/P/01.00"]
    assert report_codes("CR1AR/DC/RP/037/08") == ["CR/AR/DC/RP/037/08"]
    assert report_codes("CRITICAL/PATH/X") == []
    assert report_codes("criar/pe/rp/09/25") == []


def _block(index: int, kind: BlockKind, text: str, page: int, **extra) -> EvidenceBlock:
    level = 1 if kind is BlockKind.TITLE else 2 if kind is BlockKind.HEADING else None
    return EvidenceBlock(
        evidence_id=f"ev:{index}", kind=kind, text=text, pages=(page,), level=level, **extra
    )


def _document(pages: int = 10) -> EvidenceUnit:
    blocks: list[EvidenceBlock] = [
        _block(0, BlockKind.TITLE, HEADER, 1),
        _block(1, BlockKind.HEADING, "4.3 Acid Base Accounting", 1),
    ]
    for page in range(1, pages + 1):
        base = page * 10
        blocks += [
            _block(base, BlockKind.HEADING, HEADER, page),
            _block(base + 1, BlockKind.PARAGRAPH, "Managem", page),
            _block(base + 2, BlockKind.PAGE_HEADER, "CRIAR/PE/RP/09/25", page),
            _block(base + 3, BlockKind.PARAGRAPH, f"Results paragraph {page} on acid potential.",
                   page),
        ]
    blocks.append(_block(998, BlockKind.PARAGRAPH, "See report CR/GE/RP/94/20 for context.", 3))
    return EvidenceUnit(
        unit_id="u1", plan_id="p", content_hash="a" * 64, route="di_layout",
        extraction_method="layout", normalizer="test", blocks=tuple(blocks),
    )


def test_running_lines_and_header_codes_are_detected_document_wide() -> None:
    scan = FurnitureScan()
    scan.add(_document())
    running = scan.running()
    assert "managem" in running
    assert HEADER.casefold() in running
    assert not any("results paragraph" in key for key in running)
    assert "4.3 acid base accounting" not in running
    codes = scan.document_codes()
    assert codes[0] == "CR/AR/PE/RP/09/25"
    assert "CR/GE/RP/94/20" not in codes


def test_short_documents_never_lose_lines_to_running_detection() -> None:
    scan = FurnitureScan()
    scan.add(_document(pages=2))
    assert scan.running() == frozenset()


def test_codes_inside_tables_count_toward_the_document_code() -> None:
    table = EvidenceTable(row_count=1, column_count=2, cells=(
        EvidenceCell(row=0, column=0, text="Reminex Managem Group"),
        EvidenceCell(row=0, column=1, text="CRIAR/PE/RP/09/25"),
    ))
    blocks = [
        EvidenceBlock(evidence_id=f"t{page}", kind=BlockKind.TABLE, pages=(page,), table=table)
        for page in range(1, 5)
    ]
    scan = FurnitureScan()
    scan.add(EvidenceUnit(
        unit_id="u", plan_id="p", content_hash="a" * 64, route="di_layout",
        extraction_method="m", normalizer="n", blocks=tuple(blocks),
    ))
    assert scan.document_codes() == ("CR/AR/PE/RP/09/25",)


def test_chunker_drops_running_lines_but_keeps_title_and_real_sections() -> None:
    unit = _document()
    scan = FurnitureScan()
    scan.add(unit)
    chunker = DocumentChunker(evidence_id="evd:1", tokens=ApproximateCounter())
    chunker.running = scan.running()
    chunker.feed(unit)
    chunks = chunker.finish()
    text = "\n".join(chunk.text for chunk in chunks)
    assert "Managem" not in text.replace("Managem Group", "")
    assert {chunk.section_path for chunk in chunks} == {("4.3 Acid Base Accounting",)}
    assert chunker.title == HEADER
    assert "Results paragraph 7 on acid potential." in text

    plain = DocumentChunker(evidence_id="evd:1", tokens=ApproximateCounter())
    plain.feed(unit)
    polluted = plain.finish()
    assert "Managem" in "\n".join(chunk.text for chunk in polluted)
    assert any(HEADER in chunk.section_path for chunk in polluted)


def _snapshot(path: str) -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id="s", source_object_id="o", placement_id="p", container_name="raw",
        blob_name="crv/sources/x.pdf", etag="1", content_hash="a" * 64, size_bytes=1,
        observed_at=datetime.now(UTC),
    ).model_copy(update={"blob_name": path})


def test_header_code_outranks_cited_and_path_codes() -> None:
    chunk = EvidenceChunk(
        chunk_id="c", evidence_id="e", ordinal=0, kind=ChunkKind.PROSE,
        text="As reported in CR/GE/RP/94/20, the dose was confirmed.", token_count=9,
        tokenizer="t", chunker="t", block_ids=("b",), unit_ids=("u",), pages=(4,),
    )
    placement = [_snapshot("Documents R&D/CMG/CR-AR-XX-01-99 etude.pdf")]
    header = research_fields(chunk, placement, document_codes=("CR/AR/PE/RP/09/25",))
    assert header["document_code"] == "CR/AR/PE/RP/09/25"
    assert header["document_code_basis"] == "printed_header"
    assert header["identifiers"] == ["CR/AR/PE/RP/09/25", "CR/GE/RP/94/20", "CR-AR-XX-01-99"]
    body = research_fields(chunk, placement)
    assert (body["document_code"], body["document_code_basis"]) == ("CR/GE/RP/94/20", "printed")
    bare = chunk.model_copy(update={"text": "No code here."})
    path = research_fields(bare, placement)
    assert (path["document_code"], path["document_code_basis"]) == ("CR-AR-XX-01-99", "path")


def test_stored_zip_with_an_early_pdf_member_is_still_a_zip() -> None:
    from ingest_pipeline.domain.sniff import FileKind, sniff

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.pdf", pdf_bytes([PdfPage(lines=["x"])]))
    data = buffer.getvalue()
    assert b"%PDF-" in data[:1024]
    entries = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert sniff(data[:8192], name="bundle.zip", zip_entries=entries).kind is FileKind.ZIP
    assert sniff(b"junk\r\n%PDF-1.4\n", name="x.bin").kind is FileKind.PDF


def _ooxml(main: str, *, image_only: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr(main, "<doc/>")
        if image_only:
            archive.writestr("word/media/image1.tiff", b"II*\x00scan")
    return buffer.getvalue()


def test_ooxml_is_collected_by_content_and_converted_when_a_converter_is_set(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    docx = _ooxml("word/document.xml", image_only=True)
    pptx = _ooxml("ppt/presentation.xml")
    member = _ooxml("word/document.xml") + b"member"
    (root / "Localisation_Dist.pdf").write_bytes(docx)
    (root / "deck.pptx").write_bytes(pptx)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("BouAzzer/Historique.docx", member)
        archive.writestr("notes.pdf", pdf_bytes([PdfPage(lines=["x"])]))
    (root / "bundle.zip").write_bytes(bundle.getvalue())

    office = tmp_path / "office"
    counts = collect_legacy_office(root, office / "in")
    names = sorted(path.name for path in (office / "in").iterdir())
    assert counts == {"collected": 3, "duplicates": 0}
    assert names == sorted([
        hashlib.sha256(docx).hexdigest() + ".docx",
        hashlib.sha256(pptx).hexdigest() + ".pptx",
        hashlib.sha256(member).hexdigest() + ".docx",
    ])

    (office / "out").mkdir()
    rendered = pdf_bytes([PdfPage(image=True)])
    for name in names:
        (office / "out" / (name.split(".")[0] + ".pdf")).write_bytes(rendered)
    (office / "out" / "results.json").write_text(json.dumps({}))
    converter = PrecomputedOfficeConverter(IMAGE, office / "out")
    report = CorpusIntake(tmp_path / "intake", IntakeLimits(), converter).prepare(root)
    by_name = {item.source_path: item for item in report.items}
    for name in ("Localisation_Dist.pdf", "deck.pptx", "bundle.zip!/BouAzzer/Historique.docx"):
        assert by_name[name].status == "converted", name
        assert by_name[name].file_kind == "pdf"
        assert by_name[name].converter == IMAGE
    assert by_name["Localisation_Dist.pdf"].input_hash == hashlib.sha256(docx).hexdigest()
    assert report.failed == 0

    native = CorpusIntake(tmp_path / "native", IntakeLimits()).prepare(root)
    assert {item.source_path: item.status for item in native.items}["deck.pptx"] == "ready"
