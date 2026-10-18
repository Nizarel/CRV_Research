from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from evidence_harness import build_evidence_harness
from fixture_builders import PdfPage, Sheet, pdf_bytes, repo_file, xlsx_bytes

from tizertmaint_pipeline.adapters.parsers.calamine import CalamineSpreadsheetReader
from tizertmaint_pipeline.adapters.parsers.pdfium import PdfiumToolkit
from tizertmaint_pipeline.adapters.parsers.tokenizers import ApproximateCounter
from tizertmaint_pipeline.config import PipelineSettings, load_policies
from tizertmaint_pipeline.domain.chunks import ChunkKind, EvidenceChunk
from tizertmaint_pipeline.domain.crv import research_context
from tizertmaint_pipeline.domain.evidence import (
    BlockKind,
    EvidenceBlock,
    EvidenceCell,
    EvidenceTable,
    EvidenceUnit,
    Locator,
    LocatorKind,
)
from tizertmaint_pipeline.domain.models import SourceProvenance, SourceSnapshot, UnitRecord
from tizertmaint_pipeline.domain.observations import numeric_observations
from tizertmaint_pipeline.domain.routing import PageSignals, Route, RoutingPolicy, classify_page
from tizertmaint_pipeline.domain.states import StageName
from tizertmaint_pipeline.stages.chunkers import ChunkingSettings, table_bodies
from tizertmaint_pipeline.stages.normalizers.native import normalize_spreadsheet
from tizertmaint_pipeline.stages.project_search import ProjectionSettings, build_row


def crv_schema():
    return json.loads(repo_file("infra/search/crv-evidence-index.json").read_text())


@pytest.mark.parametrize(("path", "collection", "kind", "site"), [
    ("LIMS/Subscription order.pdf", "lims-commercial", "commercial", None),
    ("Phytoremediation/EIE TIZERT/Etude.pdf", "environment", "environmental_study", "tizert"),
    ("Documents R&D/BOUAZZER/Essai CR-AF-RP-01-21.pdf", "research", "test_report", "bou-azzer"),
    ("HSC DOC/Guide.pdf", "reference", "software_manual", None),
    ("Templates/Rapport CRV.pdf", "research", "template", None),
])
def test_research_context(path, collection, kind, site) -> None:
    context = research_context(path)
    assert context.collection == collection
    assert context.document_type == kind
    assert context.site_ids == ((site,) if site else ())


def test_projection_matches_crv_schema_and_retains_source_lineage() -> None:
    chunk = EvidenceChunk(
        chunk_id="chunk:1", evidence_id="evd:1", ordinal=0, kind=ChunkKind.PROSE,
        text="CR/AF/RP/01/21 Results", token_count=5, tokenizer="test", chunker="test",
        block_ids=("ev:1",), unit_ids=("unit:1",), pages=(7,),
    )
    source = SourceSnapshot(
        snapshot_id="s1", source_object_id="o1", placement_id="p1",
        container_name="raw", blob_name="crv/sources/opaque.pdf", etag="1",
        content_hash="a" * 64, size_bytes=100, observed_at=datetime.now(UTC),
        provenance=SourceProvenance(
            source_path="Documents R&D/BOUAZZER/Archive.zip!/Essai.doc",
            original_hash="b" * 64, original_blob="sha256/bb/original",
            archive_members=("Essai.doc",), converter="office@sha256:" + "c" * 64,
            manifest_blob="sha256/manifest",
        ),
    )
    row = build_row(
        chunk, [0.0] * 3072, placements=[source], family="pdf",
        settings=ProjectionSettings(target="crv", corpus="crv"),
    )
    fields = {field["name"] for field in crv_schema()["fields"]}
    assert set(row) <= fields
    assert row["site_ids"] == ["bou-azzer"]
    assert row["document_code"] == "CR/AF/RP/01/21"
    assert row["document_code_basis"] == "printed"
    assert row["metadata_basis"] == "path"
    assert row["citation_basis"] == "derived_pdf"
    assert row["original_page_numbers"] == [7]
    assert row["original_source_uris"] == ["raw/sha256/bb/original"]
    assert row["original_content_hashes"] == ["b" * 64]
    assert row["acl_users"] == []
    assert row["acl_groups"] == []
    assert "equipment_tags" not in row


@pytest.mark.parametrize("signal", [
    {"multi_column": True}, {"image_objects": 1}, {"path_objects": 10},
])
def test_scientific_layout_risks_override_native_text(signal) -> None:
    page = PageSignals(page=1, width_pt=595, height_pt=842, text_chars=500, **signal)
    assert classify_page(page, RoutingPolicy(preserve_layout=True)) is Route.DI_LAYOUT
    assert classify_page(page, RoutingPolicy()) is Route.NATIVE_PDF_TEXT


async def test_crv_pipeline_uses_schema_without_maintenance_fields(tmp_path: Path) -> None:
    kit = build_evidence_harness(
        tmp_path, corpus="crv", fields=frozenset(f["name"] for f in crv_schema()["fields"]),
        policy=RoutingPolicy(preserve_layout=True),
    )
    await kit.ingest("LIMS/Offre commerciale.pdf", pdf_bytes([
        PdfPage(lines=["A scientific report paragraph with sufficient native text."] * 8),
        PdfPage(image=True),
    ]))
    await kit.drain()
    assert kit.index.rows
    assert kit.jobs(StageName.NATIVE_PARSE)
    assert kit.jobs(StageName.DI_SUBMIT)
    assert {r["collection"] for r in kit.index.rows.values()} == {"lims-commercial"}
    assert {r["document_type"] for r in kit.index.rows.values()} == {"commercial"}
    assert {p for r in kit.index.rows.values() for p in r["original_page_numbers"]} == {1, 2}


async def test_flora_columns_are_separate_and_keep_absolute_cells(tmp_path: Path) -> None:
    path = tmp_path / "flora.xlsx"
    path.write_bytes(xlsx_bytes([Sheet("Flora", {
        "B1": "Species", "B2": "Tri-K", "C2": "Tizert", "D2": "Bouskour",
        "B3": "Taxon A", "C3": "Taxon B", "D3": "Taxon C", "D4": "Taxon D",
    })]))
    reader = CalamineSpreadsheetReader()
    sheets = await reader.read(path, include_hidden=True, max_cells=100)
    unit = UnitRecord(unit_id="u1", ordinal=0, kind="spreadsheet", route="native_spreadsheet")
    evidence = normalize_spreadsheet(
        sheets, unit=unit, plan_id="p1", content_hash="a" * 64,
        independent_column_header_row=2,
    )
    tables = [block.table for block in evidence.blocks]
    assert len(tables) == 3
    assert all(table and table.column_count == 1 for table in tables)
    assert [
        [cell.locator.cell for cell in table.cells if cell.locator]
        for table in tables if table
    ] == [["B1", "B2", "B3"], ["C2", "C3"], ["D2", "D3", "D4"]]
    assert [table.header_rows for table in tables if table] == [2, 1, 1]
    ordinary = normalize_spreadsheet(sheets, unit=unit, plan_id="p1", content_hash="a" * 64)
    assert ordinary.blocks[0].table.column_count == 3


async def test_oversized_pdf_windows_split_by_actual_bytes(tmp_path: Path) -> None:
    data = pdf_bytes([PdfPage(image=True) for _ in range(6)])
    source, subset = tmp_path / "input.pdf", tmp_path / "one.pdf"
    source.write_bytes(data)
    pdf = PdfiumToolkit()
    await pdf.write_subset(source, (1,), subset)
    limit = subset.stat().st_size
    kit = build_evidence_harness(tmp_path, policy=RoutingPolicy(max_submit_bytes=limit))
    await kit.ingest("scan.pdf", data)
    await kit.drain()
    assert len(kit.layout.submissions) == 6
    assert all(size <= limit for _, size in kit.layout.submissions)
    pages = {p for r in kit.index.rows.values() for p in r["original_page_numbers"]}
    assert pages == set(range(1, 7))


async def test_single_page_over_limit_never_reaches_provider(tmp_path: Path) -> None:
    kit = build_evidence_harness(tmp_path, policy=RoutingPolicy(max_submit_bytes=1))
    await kit.ingest("scan.pdf", pdf_bytes([PdfPage(image=True)]))
    await kit.drain()
    assert not kit.layout.submissions
    assert not kit.index.rows
    assert not kit.jobs(StageName.DI_SUBMIT)


def test_numeric_observations_preserve_qualifiers_and_do_not_guess_locale() -> None:
    table = EvidenceTable(
        row_count=6, column_count=2, header_rows=1, caption="Assay results",
        footnotes=("Dry basis; unreviewed OCR.",),
        cells=tuple(
            EvidenceCell(row=row, column=col, text=text)
            for row, values in enumerate([
                ("Sample", "Cu"), ("A", "<0,05 %"), ("B", "12.5 mg/kg"),
                ("C", "1,234"), ("D", "1.234"), ("E", "ND"),
            ]) for col, text in enumerate(values)
        ),
    )
    unit = EvidenceUnit(
        unit_id="u1", plan_id="p1", content_hash="a" * 64, route="di_layout",
        extraction_method="layout", normalizer="test",
        blocks=(EvidenceBlock(
            evidence_id="ev:1", kind=BlockKind.TABLE, table=table,
            locators=(Locator(kind=LocatorKind.PAGE, page=4),),
        ),),
    )
    observations = numeric_observations(unit)
    assert [o.value for o in observations] == [Decimal("0.05"), Decimal("12.5")]
    assert observations[0].raw_value == "<0,05 %"
    assert observations[0].qualifier == "<"
    assert observations[0].unit_text == "%"
    assert observations[0].row_label == "A"
    assert observations[0].column_headers == ("Cu",)
    assert observations[0].status == "unreviewed"
    assert observations[0].locators[0].page == 4
    for body, _, _ in table_bodies(table, 120, ApproximateCounter()):
        assert "Dry basis; unreviewed OCR." in body


def test_crv_configuration_and_knowledge_source_contract() -> None:
    settings = PipelineSettings()
    assert settings.blob.max_source_bytes == 1024**3
    assert settings.search.corpus == "crv"
    assert not settings.chunking.scientific_observations
    policies = load_policies(repo_file("infra/content-understanding/crv-policies.json"))
    assert not policies.get("crv-layout-v1").policy.prefer_native_text
    assert policies.get("crv-selective-v1").policy.preserve_layout
    schema = crv_schema()
    fields = {field["name"]: field for field in schema["fields"]}
    assert fields["content_vector"]["dimensions"] == 3072
    source = json.loads(repo_file("infra/search/crv-knowledge-source.json").read_text())
    params = source["searchIndexParameters"]
    assert params["searchIndexName"] == schema["name"]
    assert params["semanticConfigurationName"] == schema["semantic"]["defaultConfiguration"]
    assert params["baseFilter"].endswith("and false")
    assert all(f["name"] in fields for f in params["searchFields"] + params["sourceDataFields"])


@pytest.mark.parametrize("enabled", [False, True])
async def test_observation_artifact_is_opt_in_and_contains_cited_values(
    tmp_path: Path, enabled: bool,
) -> None:
    kit = build_evidence_harness(
        tmp_path, chunking=ChunkingSettings(scientific_observations=enabled),
    )
    await kit.ingest("CMG/results.xlsx", xlsx_bytes([Sheet("Results", {
        "A1": "Sample", "B1": "Cu", "A2": "A", "B2": "<0,05 %",
    })]))
    await kit.drain()
    [job] = kit.jobs(StageName.CHUNK)
    ledger = kit.harness.ledger
    links = dict(ledger.job_artifacts(job.job_id))
    assert ("scientific_observations" in links) is enabled
    if enabled:
        ref = ledger.artifacts()[links["scientific_observations"]]
        data = await kit.harness.services.artifacts.get(ref)
        [observation] = [json.loads(line) for line in data.splitlines()]
        assert observation["value"] == "0.05"
        assert observation["raw_value"] == "<0,05 %"
        assert observation["locators"][0]["cell"] == "B2"
        assert observation["status"] == "unreviewed"
