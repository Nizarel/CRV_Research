from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from fixture_builders import PdfPage, Sheet, pdf_bytes, xlsx_bytes

from ingest_pipeline.adapters.blob.staging import resolve, stage_blobs, staged_path, windows_key
from ingest_pipeline.adapters.local_intake import CorpusIntake, collect_legacy_office
from ingest_pipeline.adapters.parsers.office import PrecomputedOfficeConverter
from ingest_pipeline.adapters.pilot import Candidate, profile_corpus, select, selection_report
from ingest_pipeline.adapters.search.index_admin import render_schema, schema_drift
from ingest_pipeline.domain import comparison
from ingest_pipeline.domain.errors import ConfigurationError, InvalidInputError
from ingest_pipeline.domain.evidence import (
    BlockKind,
    DocumentPage,
    EvidenceBlock,
    EvidenceCell,
    EvidenceDocument,
    EvidenceTable,
    EvidenceUnit,
    PageStatus,
    UnitSummary,
)
from ingest_pipeline.domain.intake import IntakeLimits
from ingest_pipeline.domain.routing import RoutingPolicy

OLE = bytes.fromhex("d0cf11e0a1b11ae1")
IMAGE = "sha256:" + "b" * 64


class _Downloader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def chunks(self):
        for start in range(0, len(self._data), 5):
            yield self._data[start : start + 5]


class _Blob:
    def __init__(self, store: dict[str, dict[str, Any]], name: str) -> None:
        self._store, self._name = store, name

    async def get_blob_properties(self):
        blob = self._store.get(self._name)
        if blob is None:
            raise ResourceNotFoundError("missing")
        md5 = blob.get("md5", hashlib.md5(blob["data"], usedforsecurity=False).digest())
        return SimpleNamespace(
            size=len(blob["data"]), etag=blob["etag"],
            content_settings=SimpleNamespace(content_md5=bytearray(md5)),
        )

    async def download_blob(self, *, etag: str, **_: Any):
        blob = self._store[self._name]
        if etag != blob["etag"]:
            raise ResourceModifiedError("changed")
        return _Downloader(blob.get("served", blob["data"]))


class _Container:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = {name: {"data": data, "etag": '"1"'} for name, data in blobs.items()}
        self.requested: list[str] = []

    def get_blob_client(self, name: str) -> _Blob:
        self.requested.append(name)
        return _Blob(self.blobs, name)

    async def list_blobs(self, *, name_starts_with: str = ""):
        for name in sorted(self.blobs):
            if name.startswith(name_starts_with):
                yield SimpleNamespace(name=name)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


async def test_stage_blobs_mirrors_layout_extracts_members_and_reports_failures(
    tmp_path: Path,
) -> None:
    pdf = pdf_bytes([PdfPage(lines=["Rapport"])])
    sheet = xlsx_bytes([Sheet("Flora", {"B1": "Species"})])
    container = _Container({
        "DATAROOM CRV/Documents R&D/rapport.pdf": pdf,
        "DATAROOM CRV/Phyto/bundle.zip": _zip({"Données/Flore.xlsx": sheet, "other.pdf": pdf}),
        "DATAROOM CRV/bad.pdf": b"tampered",
    })
    container.blobs["DATAROOM CRV/bad.pdf"]["served"] = b"tamperex"
    out = tmp_path / "corpus"
    report = await stage_blobs(
        container,
        [
            "Documents R&D/rapport.pdf", "Phyto/bundle.zip!/Données/Flore.xlsx",
            "missing.pdf", "bad.pdf", "../escape.pdf", "Phyto/bundle.zip!/absent.xlsx",
        ],
        out, prefix="DATAROOM CRV/", max_bytes=10_000_000,
    )
    assert (out / "Documents R&D" / "rapport.pdf").read_bytes() == pdf
    assert staged_path(out, "Phyto/bundle.zip!/Données/Flore.xlsx").read_bytes() == sheet
    assert [item["entry"] for item in report["staged"]] == [
        "Documents R&D/rapport.pdf", "Phyto/bundle.zip!/Données/Flore.xlsx",
    ]
    assert report["staged"][0]["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert report["staged"][0]["md5_verified"]
    reasons = {item["entry"]: item["reason"] for item in report["failed"]}
    assert set(reasons) == {
        "missing.pdf", "bad.pdf", "../escape.pdf", "Phyto/bundle.zip!/absent.xlsx",
    }
    assert "Content-MD5" in reasons["bad.pdf"]
    assert not (out / "bad.pdf").exists()
    assert not list(tmp_path.glob("corpus.archives/*.zip"))  # noqa: ASYNC240 - test assertion
    assert container.requested.count("DATAROOM CRV/Phyto/bundle.zip") == 1


async def test_stage_blobs_without_a_list_stages_every_blob_under_its_original_name(
    tmp_path: Path,
) -> None:
    pdf = pdf_bytes([PdfPage(lines=["Rapport"])])
    container = _Container({
        "DATAROOM CRV/Données R&D/rapport.pdf": pdf,
        "DATAROOM CRV/Phyto/bundle.zip": _zip({"other.pdf": pdf}),
        "_transfer-reports/run.json": b"{}",
    })
    out = tmp_path / "corpus"
    report = await stage_blobs(container, None, out, prefix="DATAROOM CRV/", max_bytes=10**7)
    assert not report["failed"]
    sources = [item["source"] for item in report["staged"]]
    assert sources == ["Données R&D/rapport.pdf", "Phyto/bundle.zip"]
    assert (out / "Données R&D" / "rapport.pdf").read_bytes() == pdf
    assert zipfile.is_zipfile(out / "Phyto" / "bundle.zip")


async def test_stage_blobs_rejects_oversized_sources(tmp_path: Path) -> None:
    container = _Container({"big.pdf": b"x" * 100})
    report = await stage_blobs(container, ["big.pdf"], tmp_path / "c", prefix="", max_bytes=10)
    assert report["failed"][0]["entry"] == "big.pdf"
    assert "staging limit" in report["failed"][0]["reason"]


def test_resolve_maps_windows_renamed_paths_and_rejects_ambiguity() -> None:
    names = [
        "AI for Science: Geology/Bibliothèque: A/rapport.pdf",
        "AI for Science: Geology/bundle.zip",
        "Twin: A/x.pdf", "Twin? A/x.pdf", "exact_name.pdf", "exact:name.pdf",
    ]
    index: dict[str, list[str]] = {}
    for name in names:
        index.setdefault(windows_key(name), []).append(name)
    assert resolve("AI for Science_ Geology/Bibliothèque_ A/rapport.pdf", index) == names[0]
    assert resolve("AI for Science_ Geology/bundle.zip!/Données/Flore.xlsx", index) == (
        "AI for Science: Geology/bundle.zip!/Données/Flore.xlsx"
    )
    assert resolve("exact_name.pdf", index) == "exact_name.pdf"
    with pytest.raises(InvalidInputError, match="ambiguous"):
        resolve("Twin_ A/x.pdf", index)
    with pytest.raises(InvalidInputError, match="not found"):
        resolve("absent.pdf", index)


@pytest.mark.skipif(os.name == "nt", reason="original names contain ':' (staging runs on Linux)")
async def test_stage_blobs_resolves_windows_renamed_paths_to_original_names(
    tmp_path: Path,
) -> None:
    pdf = pdf_bytes([PdfPage(lines=["Etude"])])
    sheet = xlsx_bytes([Sheet("Flora", {"B1": "Species"})])
    container = _Container({
        "DATAROOM CRV/AI for Science: Geology/Bibliothèque: A/rapport.pdf": pdf,
        "DATAROOM CRV/AI for Science: Geology/bundle.zip": _zip({"Flore.xlsx": sheet}),
        "DATAROOM CRV/Twin: A/x.pdf": pdf,
        "DATAROOM CRV/Twin? A/x.pdf": pdf,
    })
    out = tmp_path / "corpus"
    report = await stage_blobs(
        container,
        [
            "AI for Science_ Geology/Bibliothèque_ A/rapport.pdf",
            "AI for Science_ Geology/bundle.zip!/Flore.xlsx",
            "Twin_ A/x.pdf",
        ],
        out, prefix="DATAROOM CRV/", max_bytes=10_000_000,
    )
    sources = {item["entry"]: item["source"] for item in report["staged"]}
    assert sources == {
        "AI for Science_ Geology/Bibliothèque_ A/rapport.pdf":
            "AI for Science: Geology/Bibliothèque: A/rapport.pdf",
        "AI for Science_ Geology/bundle.zip!/Flore.xlsx":
            "AI for Science: Geology/bundle.zip!/Flore.xlsx",
    }
    assert (out / "AI for Science: Geology" / "Bibliothèque: A" / "rapport.pdf").read_bytes() == pdf
    assert staged_path(out, "AI for Science: Geology/bundle.zip!/Flore.xlsx").read_bytes() == sheet
    [ambiguous] = report["failed"]
    assert ambiguous["entry"] == "Twin_ A/x.pdf"
    assert "ambiguous" in ambiguous["reason"]


def test_collect_and_precomputed_conversion_feed_intake(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "reports").mkdir(parents=True)
    doc, ppt = OLE + b"word", OLE + b"slides"
    (root / "reports" / "a.doc").write_bytes(doc)
    (root / "reports" / "copy.doc").write_bytes(doc)
    (root / "deck.ppt").write_bytes(ppt)
    (root / "notes.doc").write_bytes(b"plain text pretending")
    office = tmp_path / "office"
    counts = collect_legacy_office(root, office / "in")
    assert counts == {"collected": 2, "duplicates": 1}
    doc_hash, ppt_hash = hashlib.sha256(doc).hexdigest(), hashlib.sha256(ppt).hexdigest()
    assert sorted(path.name for path in (office / "in").iterdir()) == sorted(
        [f"{doc_hash}.doc", f"{ppt_hash}.ppt"]
    )
    converted = pdf_bytes([PdfPage(lines=["converted"])])
    (office / "out").mkdir()
    (office / "out" / f"{doc_hash}.pdf").write_bytes(converted)
    (office / "out" / "results.json").write_text(json.dumps({
        doc_hash: {"status": "converted"},
        ppt_hash: {"status": "failed", "reason": "LibreOffice exit 1; no PDF produced"},
    }))
    converter = PrecomputedOfficeConverter(IMAGE, office / "out")
    report = CorpusIntake(tmp_path / "intake", IntakeLimits(), converter).prepare(root)
    by_name = {item.source_path: item for item in report.items}
    for name in ("reports/a.doc", "reports/copy.doc"):
        item = by_name[name]
        assert item.status == "converted"
        assert item.converter == IMAGE
        assert item.input_hash == doc_hash
        assert item.content_hash == hashlib.sha256(converted).hexdigest()
    assert by_name["deck.ppt"].status == "failed"
    assert "LibreOffice exit 1" in (by_name["deck.ppt"].reason or "")
    assert report.failed == 1


def test_precomputed_converter_rejects_unpinned_images_and_non_pdf(tmp_path: Path) -> None:
    source = tmp_path / "a.doc"
    source.write_bytes(OLE)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ConfigurationError, match="pinned"):
        PrecomputedOfficeConverter("office:latest", out).convert(source, tmp_path / "x", ".doc")
    (out / f"{hashlib.sha256(OLE).hexdigest()}.pdf").write_bytes(b"not a pdf")
    with pytest.raises(Exception, match="did not produce a PDF"):
        PrecomputedOfficeConverter(IMAGE, out).convert(source, tmp_path / "x", ".doc")


def test_collect_legacy_office_refuses_output_inside_corpus(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        collect_legacy_office(tmp_path, tmp_path / "office")


async def test_pilot_selection_is_deterministic_stratified_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    for folder in ("Documents R&D", "LIMS", "Templates", "Bibliotheque"):
        (root / folder).mkdir(parents=True)
    scan = pdf_bytes([PdfPage(image=True)] * 3)
    text = pdf_bytes([PdfPage(lines=["Paragraph of research text."] * 12)] * 2)
    for index in range(6):
        (root / "Documents R&D" / f"essai-{index}.pdf").write_bytes(scan)
    (root / "LIMS" / "Offre commerciale.pdf").write_bytes(text)
    (root / "Templates" / "Rapport CRV.pdf").write_bytes(text)
    (root / "Bibliotheque" / "k.Dachri et al 2024.pdf").write_bytes(text)
    (root / "Documents R&D" / "old.doc").write_bytes(OLE + b"legacy")
    (root / "misnamed.docx").write_bytes(text)
    (root / "word-as.pdf").write_bytes(_zip({
        "[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<Relationships/>",
        "word/document.xml": b"<w:document/>", "word/media/image1.tiff": b"II*\x00",
    }))
    (root / "Phyto.zip").write_bytes(_zip({"Flore.xlsx": xlsx_bytes([Sheet("F", {"B1": "x"})])}))
    (root / "Readme.md").write_text("")
    policy = RoutingPolicy(version="crv-layout-v1", preserve_layout=True, prefer_native_text=False)
    candidates = await profile_corpus(root, policy)
    by_entry = {item.entry: item for item in candidates}
    assert by_entry["Documents R&D/essai-0.pdf"].di_pages == 3
    assert by_entry["Documents R&D/essai-0.pdf"].stratum == "scanned_report"
    assert by_entry["LIMS/Offre commerciale.pdf"].stratum == "lims_commercial"
    assert by_entry["Templates/Rapport CRV.pdf"].stratum == "procedure_template"
    assert by_entry["Bibliotheque/k.Dachri et al 2024.pdf"].stratum == "two_column_paper"
    assert by_entry["Documents R&D/old.doc"].di_pages is None
    assert by_entry["misnamed.docx"].stratum == "mislabeled"
    assert by_entry["word-as.pdf"].kind == "docx"
    assert by_entry["word-as.pdf"].stratum == "mislabeled"
    assert not any(entry.startswith("word-as.pdf!/") for entry in by_entry)
    assert by_entry["Phyto.zip!/Flore.xlsx"].stratum == "spreadsheet"
    assert by_entry["Phyto.zip!/Flore.xlsx"].di_pages == 0

    first = select(candidates, target_pages=12, seed=7, must_include=["Readme.md"])
    second = select(candidates, target_pages=12, seed=7, must_include=["Readme.md"])
    assert [item.entry for item in first.entries] == [item.entry for item in second.entries]
    strata = {item.stratum for item in first.entries}
    assert {"lims_commercial", "procedure_template", "legacy_office", "mislabeled",
            "spreadsheet", "two_column_paper", "scanned_report"} <= strata
    assert first.di_pages <= 12 * 1.1 + 3
    assert first.unknown == ["Documents R&D/old.doc"]
    report = selection_report(first, di_page_usd=0.01, cu_page_usd=0.005)
    assert report["predicted_usd"]["di_layout"] == round(first.di_pages * 0.01, 2)
    with pytest.raises(ValueError, match="not found"):
        select(candidates, target_pages=5, seed=1, must_include=["absent.pdf"])


def test_pilot_fill_skips_documents_over_the_page_cap() -> None:
    def item(name: str, pages: int) -> Candidate:
        return Candidate(name, "pdf", "research_report", "research", 1, pages)

    candidates = [item("huge.pdf", 500), item("a.pdf", 10), item("b.pdf", 10)]
    chosen = select(candidates, target_pages=100, seed=1, max_document_pages=120)
    assert "huge.pdf" not in {entry.entry for entry in chosen.entries}
    forced = select(candidates, target_pages=100, seed=1, must_include=["huge.pdf"])
    assert "huge.pdf" in {entry.entry for entry in forced.entries}


def test_index_schema_rendering_requires_every_value_and_matching_name(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({
        "name": "crv-evidence-v1",
        "fields": [{"name": "id", "type": "Edm.String", "key": True}],
        "vectorizer": {"uri": "${URI}", "deployment": "${DEPLOYMENT}"},
    }))
    schema = render_schema(path, {"URI": "https://x/", "DEPLOYMENT": "emb"},
                           index_name="crv-evidence-v1")
    assert schema["vectorizer"] == {"uri": "https://x/", "deployment": "emb"}
    with pytest.raises(ConfigurationError, match="DEPLOYMENT"):
        render_schema(path, {"URI": "https://x/"}, index_name="crv-evidence-v1")
    with pytest.raises(ConfigurationError, match="configured target"):
        render_schema(path, {"URI": "a", "DEPLOYMENT": "b"}, index_name="other")
    with pytest.raises(ConfigurationError, match="Invalid value"):
        render_schema(path, {"URI": 'a"b', "DEPLOYMENT": "b"}, index_name="crv-evidence-v1")


def test_schema_drift_reports_projection_relevant_differences() -> None:
    expected = {"fields": [
        {"name": "id", "type": "Edm.String", "key": True},
        {"name": "v", "type": "Collection(Edm.Single)", "dimensions": 3072,
         "vectorSearchProfile": "p"},
        {"name": "title", "type": "Edm.String"},
    ]}
    actual = {"fields": [
        {"name": "id", "type": "Edm.String", "key": True},
        {"name": "v", "type": "Collection(Edm.Single)", "dimensions": 1536,
         "vectorSearchProfile": "p"},
        {"name": "legacy", "type": "Edm.String"},
    ]}
    drift = schema_drift(expected, actual)
    assert "missing field title" in drift
    assert "unexpected field legacy" in drift
    assert any(item.startswith("field v differs") for item in drift)
    assert schema_drift(expected, expected) == []


def _document(content: str, statuses: dict[int, PageStatus], unit_status: str) -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id=f"evd:{content}", plan_id="p", content_hash=content * 64,
        policy_version="v", family="pdf", page_count=len(statuses),
        units=(UnitSummary(unit_id="u", ordinal=0, route="r", kind="k", disposition=unit_status),),
        pages=tuple(DocumentPage(page=page, status=status) for page, status in statuses.items()),
        completeness="complete" if unit_status == "evidence" else "partial",
    )


def _unit(texts: dict[int, str], table: bool = False) -> EvidenceUnit:
    blocks = [
        EvidenceBlock(evidence_id=f"e{page}", kind=BlockKind.PARAGRAPH, text=text, pages=(page,))
        for page, text in texts.items()
    ]
    if table:
        blocks.append(EvidenceBlock(
            evidence_id="t", kind=BlockKind.TABLE, pages=(1,),
            table=EvidenceTable(row_count=1, column_count=2, cells=(
                EvidenceCell(row=0, column=0, text="Cu"), EvidenceCell(row=0, column=1, text="12"),
            )),
        ))
    return EvidenceUnit(
        unit_id="u", plan_id="p", content_hash="a" * 64, route="r",
        extraction_method="m", normalizer="n", blocks=tuple(blocks),
    )


def test_comparison_reports_coverage_tables_failures_and_review_sample() -> None:
    di, cu = comparison.ArmStats(), comparison.ArmStats()
    statuses = {1: PageStatus.CONTENT, 2: PageStatus.CONTENT}
    comparison.collect(di, "a", _document("a", statuses, "evidence"),
                       [_unit({1: "x" * 100, 2: "y" * 100}, table=True)])
    comparison.collect(cu, "a", _document("a", statuses, "failed"), [_unit({1: "x" * 100})])
    comparison.collect(cu, "b", None, [])
    di_summary = comparison.summary(di, pages_billed=2, page_usd=0.01)
    cu_summary = comparison.summary(cu, pages_billed=2, page_usd=0.005)
    assert (di_summary["tables"], di_summary["table_cells"]) == (1, 2)
    assert di_summary["pages_with_text"] == 2
    assert cu_summary["pages_with_text"] == 1
    assert cu_summary["failed_or_unsupported_units"] == 1
    assert cu_summary["missing_evidence"] == 1
    assert cu_summary["estimated_usd"] == 0.01
    assert comparison.divergent_pages(di, cu) == [("a", 2, 100, 0)]
    sample = comparison.review_sample(di, cu, {"a": "src/a.pdf"}, size=2, seed=3)
    assert sample[0] == {"content_hash": "a", "source": "src/a.pdf", "page": 2,
                         "chars_a": 100, "chars_b": 0, "divergent": True}
    assert len(sample) == 2
    assert sample == comparison.review_sample(di, cu, {"a": "src/a.pdf"}, size=2, seed=3)


def test_invalid_staging_entries_are_rejected() -> None:
    with pytest.raises(InvalidInputError):
        staged_path(Path("/w"), "/abs.pdf")
    with pytest.raises(InvalidInputError):
        staged_path(Path("/w"), "a.zip!/../x")


def test_converter_batch_records_every_outcome(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("resource")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "office_convert", Path(__file__).parents[2] / "office-converter" / "convert.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inputs, outputs = tmp_path / "in", tmp_path / "out"
    inputs.mkdir()
    outputs.mkdir()
    good, bad = "a" * 64, "b" * 64
    (inputs / f"{good}.doc").write_bytes(OLE)
    (inputs / f"{bad}.ppt").write_bytes(OLE)
    (inputs / "ignored.txt").write_bytes(b"x")

    def fake(source: Path, outdir: Path, profile: Path) -> int:
        outdir.mkdir(parents=True)
        if source.name.startswith(good):
            (outdir / f"{good}.pdf").write_bytes(b"%PDF-1.7 converted")
            return 0
        return 1

    monkeypatch.setattr(module, "convert", fake)
    monkeypatch.setattr(module.pathlib, "Path", type(tmp_path))
    assert module.batch(inputs, outputs) == 0
    results = json.loads((outputs / "results.json").read_text())
    assert results[good] == {"status": "converted"}
    assert results[bad]["status"] == "failed"
    assert (outputs / f"{good}.pdf").read_bytes().startswith(b"%PDF-")
