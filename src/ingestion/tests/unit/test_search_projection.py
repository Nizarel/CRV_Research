"""Index v2 rows: path and field metadata, and the contract with the shipped schema."""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fixture_builders import index_schema, repo_file

from tizertmaint_pipeline.config import EmbeddingSettings
from tizertmaint_pipeline.domain.chunks import ChunkKind, EvidenceChunk
from tizertmaint_pipeline.domain.document_classes import (
    DOCUMENT_CLASSES,
    classify_document_category,
    classify_engineering_number,
    classify_file_name,
)
from tizertmaint_pipeline.domain.evidence import EvidenceKind, FieldObservation
from tizertmaint_pipeline.domain.models import SourceSnapshot
from tizertmaint_pipeline.domain.process_cases import process_case_id
from tizertmaint_pipeline.stages.project_search import (
    ProjectionSettings,
    build_row,
    canonical_order,
)
from tizertmaint_pipeline.stages.projection_fields import (
    MAX_TITLE_CHARS,
    UnitMetadata,
    placement_metadata,
    row_metadata,
)

DOSSIER = (
    "1-Zone Stockage & concassage/1-Crushing Feed Circuit/18-Conveyor ROM 6028-CV-1037/"
    "1-Plans/SP0904-0000-1S18-011 6028-CV-1037 (ROM Stockpile Discharge CVR) Rev00/DWG/"
    "[S] - BR21 (A3) 01-00.dwg"
)
KNOWLEDGE_SOURCE = "infra/search/evidence-v2-knowledge-source.json"
PREDEFINED_ANALYZERS = {"standard.lucene", "fr.microsoft", "en.microsoft"}
PREDEFINED_TOKENIZERS = {"keyword_v2"}
ACCESS_FIELDS = {"access_scope_id", "acl_groups", "acl_users", "policy_version"}


def _chunk(text: str = "Pump body", **changes: Any) -> EvidenceChunk:
    values: dict[str, Any] = {
        "chunk_id": "chunk:1",
        "evidence_id": "evd:1",
        "ordinal": 0,
        "kind": ChunkKind.PROSE,
        "text": text,
        "token_count": 3,
        "tokenizer": "approx",
        "chunker": "test@1",
        "block_ids": ("ev:1",),
        "unit_ids": ("u1",),
        **changes,
    }
    return EvidenceChunk(**values)


def _field(
    path: str,
    value: Any,
    pages: tuple[int, ...] = (),
    kind: EvidenceKind = EvidenceKind.EXTRACTED_FIELD,
) -> FieldObservation:
    return FieldObservation(
        path=path,
        field=re.split(r"[\[.]", path)[0],
        method="extract" if kind is EvidenceKind.EXTRACTED_FIELD else "generate",
        evidence_kind=kind,
        value_type="string",
        value=value,
        pages=pages,
        analyzer_id="test",
    )


def _snapshot(blob_name: str = DOSSIER, placement: str = "plc:1") -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=f"snap:{placement}",
        source_object_id=f"obj:{placement}",
        placement_id=placement,
        container_name="raw",
        blob_name=blob_name,
        etag="e",
        content_hash="a" * 64,
        size_bytes=1,
        observed_at=datetime(2026, 10, 9, tzinfo=UTC),
    )


def _rows(units: dict[str, UnitMetadata], chunk: EvidenceChunk, blob: str = DOSSIER) -> Any:
    return row_metadata(chunk, placements=[placement_metadata(blob)], units=units, family="pdf")


def _fields(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in schema["fields"]}


def test_placement_metadata_reads_zone_dossier_tags_and_codes() -> None:
    placement = placement_metadata(DOSSIER)

    assert placement.zone_name == "Zone Stockage & concassage"
    assert placement.equipment_folder == "Conveyor ROM 6028-CV-1037"
    assert placement.path_tags == ("6028-CV-1037",)
    assert placement.path_document_codes == ("SP0904-0000-1S18-011",)
    assert placement.file_extension == ".dwg"
    assert placement.source_path.startswith("1-Zone Stockage & concassage / 1-Crushing Feed")
    assert placement.document_class == "plan"


def test_placement_metadata_of_pilot_and_flat_paths() -> None:
    pilot = placement_metadata("_pilot/x/N°2_6520-CV-5237_Manual.PDF")
    assert pilot.zone_name is None
    assert pilot.equipment_folder is None
    assert pilot.path_tags == ("6520-CV-5237",)
    assert pilot.source_path == "pilot / x / N°2 6520-CV-5237 Manual.PDF"
    assert pilot.file_extension == ".pdf"
    assert pilot.document_class == "unclassified"
    assert placement_metadata("README").file_extension == ""


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("1-Plans", "plan"),
        ("2-Manuel d'utilisation", "manufacturer_manual"),
        ("3-Fiche techniques", "technical_datasheet"),
        ("4-BOM", "bom"),
        ("5-Modes Opératoires", "operating_procedure"),
        ("6-Gammes préventives", "preventive_maintenance"),
        ("7-Shcéma élec & instrum", "electrical_instrumentation_schematic"),
        ("7-Shéma élec & instrum", "electrical_instrumentation_schematic"),
        ("18-Instruments", "unclassified"),
        ("8-PMP & Planning Lubrification", "lubrication"),
        ("7-Planning lubrification", "lubrication"),
        ("9-Certificats", "certificate"),
        ("7-Modèle", "model"),
        ("7-Modele", "model"),
        ("2-Process plant piping", "unclassified"),
        ("9-Documents METSO", "unclassified"),
        (None, "unclassified"),
    ],
)
def test_category_folders_map_to_document_classes(label: str | None, expected: str) -> None:
    assert classify_document_category(label) == expected
    assert expected in DOCUMENT_CLASSES


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        ("12-Pont bascule/2-Manuels constructeurs/balance.pdf", "manufacturer_manual"),
        (
            "10-Installation électrique & Instrumentation/13-Schéma RIO PANEL/rio.pdf",
            "electrical_instrumentation_schematic",
        ),
        ("14-Conduites/2-Process plant piping/1-Plans/mto.pdf", "plan"),
        (
            "2-Zone Broyage/2-Grinding 2/Ball Mill_6220-ML-2242/9-Documents METSO/1-Plans/x.pdf",
            "unclassified",
        ),
        ("2-Zone Broyage/2-Grinding 2/Ball Mill_6220-ML-2242/direct.pdf", "unclassified"),
        (
            "10-Installation électrique & Instrumentation/18-Instruments/Flow instrument/"
            "34491 - 202410301412/SP0904-6300-1I07-SCHD-15001_028-Rev01.xlsx",
            "electrical_instrumentation_schematic",
        ),
        (
            "10-Installation électrique & Instrumentation/21-Eclairage/"
            "SP0904-6200-E-LAYT-22001_001-REV00.pdf",
            "electrical_instrumentation_schematic",
        ),
        (
            "12-Pont bascule/2-Manuels constructeurs/SP0904-6100-I-CODG-07000.pdf",
            "manufacturer_manual",
        ),
        (
            "14-Conduites/3-Conduites rigides/SP0904-P810.00-G02-0010 Sub 01 Status A.pdf",
            "plan",
        ),
        (
            "14-Conduites/2-Process plant piping/"
            "SP0904-0000-1P33-001 - Process Plant Piping MTO (Rev 0A).xlsx",
            "bom",
        ),
        ("15-Laboratoire/1-Compresseur/30710720.pdf", "unclassified"),
    ],
)
def test_document_class_reads_the_dossier_category_or_the_shallowest_category(
    blob: str, expected: str
) -> None:
    assert placement_metadata(blob).document_class == expected
    assert _rows({}, _chunk(), blob=blob)["document_class"] == expected


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("SP0904-6000-1I07-SCHD-29001_000-Rev 01.pdf", "electrical_instrumentation_schematic"),
        ("SP0904-6100-I-BLOC-01001_001-REV00.pdf", "electrical_instrumentation_schematic"),
        ("sp0904-6000-e-sldg-04001_001-rev02.pdf", "electrical_instrumentation_schematic"),
        ("SP0904-6028-M-MODL-20111_005-REV01.dwg", "plan"),
        ("SP0904-6028-S-DETL-35007_001-REV01.pdf", "plan"),
        ("SP0904-6100-C-LAYT-10001_001-REV00.pdf", "unclassified"),
        ("SP0904-I713.00-I17-0017 Sub 02 Status B.pdf", "unclassified"),
        ("XSP0904-6100-I-BLOC-01001.pdf", "unclassified"),
        ("IMG_20240902_112854.jpg", "unclassified"),
        ("", "unclassified"),
    ],
)
def test_engineering_number_discipline_classifies_files_without_a_category_folder(
    file_name: str, expected: str
) -> None:
    assert classify_engineering_number(file_name) == expected
    assert expected in DOCUMENT_CLASSES


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("SP0904-6028-S-DETL-35007_001-REV01.pdf", "plan"),
        ("SP0904-P810.00-G02-0010 Sub 01 Status A.pdf", "plan"),
        ("SP0904-M016.00-G05-0003 Sub 01 Status B.pdf", "manufacturer_manual"),
        ("SP0904-P801.01-G15-0009.pdf", "bom"),
        ("SP0904-M016.00-G19-0010 Sub 01 Status B.pdf", "calculation_note"),
        ("SP0904-M045.00-Q06-0046 Sub 01 Status A.pdf", "certificate"),
        ("sp0904-p810.00-q03-0003 sub 01 status a.pdf", "certificate"),
        ("SP0904-I713.00-I17-0017 Sub 02 Status B.pdf", "unclassified"),
        ("SP0904-P810.00-Q01-0002 Sub 01 Status A.pdf", "unclassified"),
        ("SP0904-6300-1M33-371_REV00 - MTO - 6330-ZA-3371.xls", "bom"),
        (
            "SP0904-4000-1D06-001 - Backfill Plant - Paste Pump Dutysheet - Rev 00.pdf",
            "technical_datasheet",
        ),
        ("6028-CV-1037 Drive Assembly Data Sheet.pdf", "technical_datasheet"),
        ("FICHE TECHNIQUE POMPE ROVATTI.pdf", "technical_datasheet"),
        ("Manuel d'utilisation.pdf", "manufacturer_manual"),
        ("Certificat de conformité.pdf", "certificate"),
        ("Spare parts list.pdf", "bom"),
        ("Summary of Comments on SP0904-P810.00-G02-0033 Sub 01 Status B.pdf", "plan"),
        ("MECHANICAL and ELECTRICAL WORKSHOP_REV07.pdf", "unclassified"),
        ("IMG_20240902_112854.jpg", "unclassified"),
        ("", "unclassified"),
    ],
)
def test_file_name_declares_the_class_of_files_without_a_category_folder(
    file_name: str, expected: str
) -> None:
    assert classify_file_name(file_name) == expected
    assert expected in DOCUMENT_CLASSES


def test_extracted_fields_on_the_chunk_pages_fill_codes_tags_and_title() -> None:
    unit = UnitMetadata(
        route="cu_analyze",
        kind="drawing_sheet",
        fields=(
            _field("DocumentCode", "sp0904-6520-p-001", (1,)),
            _field("Revision", "B", (1,)),
            _field("IssueStatus", "IFC", (1,)),
            _field("Title", "General arrangement", (1,)),
            _field("EquipmentTags[0]", "6520-cv-5237", (1,)),
            _field("EquipmentTags[1]", "6520-CV-9999", (2,)),
            _field("StreamNumbers[0]", "161", (1,)),
            _field("Revision", "Z", (1,), EvidenceKind.GENERATED_DESCRIPTION),
        ),
    )
    chunk = _chunk("Pompe 6520-PU-1234 de reprise", pages=(1,), language="fr")

    row = _rows({"u1": unit}, chunk)

    assert row["document_code"] == "SP0904-6520-P-001"
    assert row["document_code_basis"] == "printed"
    assert row["revision"] == "B"
    assert row["issue_status"] == "IFC"
    assert row["mentioned_tags"] == ["6520-CV-5237", "6520-PU-1234"]
    assert row["equipment_tags"] == ["6520-CV-5237", "6520-PU-1234", "6028-CV-1037"]
    assert row["identifiers"][:3] == ["SP0904-6520-P-001", "6520-CV-5237", "6520-PU-1234"]
    assert {"161", "6028-CV-1037", "SP0904-0000-1S18-011"} <= set(row["identifiers"])
    assert "6520-CV-9999" not in row["identifiers"]
    assert row["title"] == "General arrangement — Conveyor ROM 6028-CV-1037 — p. 1"
    assert (row["unit_kind"], row["extraction_route"]) == ("drawing_sheet", "cu_analyze")
    assert (row["content_fr"], row["content_en"]) == (chunk.text, None)


def test_dwg_title_block_fills_part_mark_revision_and_sheet() -> None:
    unit = UnitMetadata(
        route="native_dwg",
        kind="cad_drawing",
        fields=(
            _field("TitleBlock[0].POS_NUM", "b39"),
            _field("TitleBlock[0].REVISION_INDEX", "02"),
        ),
    )
    chunk = _chunk(
        "Title block POS_NUM: B39",
        kind=ChunkKind.DRAWING_TEXT,
        locators=("sheet:Model", "sheet:A3 Advance Steel"),
        language="en",
    )

    row = _rows({"u1": unit}, chunk)

    assert row["drawing_sheet_id"] == "sheet:A3 Advance Steel"
    assert row["revision"] == "02"
    assert "B39" in row["identifiers"]
    assert (row["document_code"], row["document_code_basis"]) == ("SP0904-0000-1S18-011", "path")
    assert row["title"].endswith("— sheet A3 Advance Steel")
    assert row["title"].startswith("[S] - BR21 (A3) 01-00 — Conveyor ROM")
    assert (row["content_fr"], row["content_en"]) == (None, chunk.text)


def test_partial_printed_numbers_fall_back_to_the_path_and_drop_their_revision() -> None:
    unit = UnitMetadata(
        route="cu_analyze",
        kind="drawing_sheet",
        fields=(
            _field("DrawingNumber", "PFDS 001 00", (22,)),
            _field("Revision", "00", (22,)),
            _field("IssueStatus", "ISSUED FOR DESIGN", (22,)),
        ),
    )

    row = _rows({"u1": unit}, _chunk(pages=(22,)))

    assert (row["document_code"], row["document_code_basis"]) == ("SP0904-0000-1S18-011", "path")
    assert row["revision"] is None
    assert row["issue_status"] == "ISSUED FOR DESIGN"
    assert "PFDS 001 00" not in row["identifiers"]


def test_sheet_size_is_not_a_revision_and_scale_is_not_an_issue_status() -> None:
    unit = UnitMetadata(
        route="cu_analyze",
        kind="drawing_sheet",
        fields=(
            _field("DrawingNumber", "SP0904-6600-D-PFDS-003", (24,)),
            _field("Revision", "A1", (24,)),
            _field("IssueStatus", "N.T.S.", (24,)),
        ),
    )

    row = _rows({"u1": unit}, _chunk(pages=(24,)))

    assert (row["document_code"], row["document_code_basis"]) == (
        "SP0904-6600-D-PFDS-003",
        "printed",
    )
    assert row["revision"] is None
    assert row["issue_status"] is None


def test_process_case_comes_from_the_chunk() -> None:
    case = process_case_id("with-backfill")

    assert _rows({}, _chunk(process_case_id=case))["process_case_id"] == case
    assert _rows({}, _chunk())["process_case_id"] is None


def test_rows_without_fields_or_language_stay_searchable_in_both_languages() -> None:
    row = _rows({}, _chunk(pages=(3, 4, 5)), blob="_pilot/manual.pdf")

    assert row["content_fr"] == row["content_en"] == "Pump body"
    assert row["document_code"] is None
    assert row["document_code_basis"] is None
    assert row["title"] == "manual — pp. 3-5"
    assert row["unit_kind"] is None
    assert row["drawing_sheet_id"] is None


def test_title_is_bounded_and_does_not_repeat_the_folder() -> None:
    unit = UnitMetadata(
        route="cu_analyze",
        kind="drawing_sheet",
        fields=(_field("Title", "Conveyor ROM 6028-CV-1037 " + "x" * 400),),
    )
    title = _rows({"u1": unit}, _chunk())["title"]

    assert len(title) == MAX_TITLE_CHARS
    assert title.endswith("…")
    assert title.count("Conveyor ROM") == 1


def test_build_row_fills_exactly_the_schema_fields() -> None:
    schema = index_schema()
    row = build_row(
        _chunk(),
        (0.0,) * 3,
        placements=[_snapshot()],
        family="cad",
        settings=ProjectionSettings(target="t"),
    )

    assert set(row) == set(_fields(schema))


def test_one_row_lists_every_placement_and_the_shortest_path_names_it() -> None:
    copy = "3-Zone Flottation/1-Rougher/Cell N°2_6310-FC-3122/3-Fiches techniques/a.pdf"
    canonical = "3-Zone Flottation/Cell N°1_6310-FC-3121/1-Plans/a.pdf"
    placements = canonical_order([_snapshot(copy, "plc:2"), _snapshot(canonical, "plc:1")])
    settings = ProjectionSettings(target="t")

    row = build_row(_chunk(), (0.0,) * 3, placements=placements, family="pdf", settings=settings)

    assert [s.placement_id for s in placements] == ["plc:1", "plc:2"]
    assert row["placement_id"] == "plc:1"
    assert row["placement_ids"] == ["plc:1", "plc:2"]
    assert row["source_uri"] == f"raw/{canonical}"
    assert row["equipment_folder"] == "Cell N°1 6310-FC-3121"
    assert row["equipment_folders"] == ["Cell N°1 6310-FC-3121", "Cell N°2 6310-FC-3122"]
    assert row["zone_names"] == ["Zone Flottation"]
    assert row["document_class"] == "plan"
    assert row["document_classes"] == ["plan", "technical_datasheet"]
    assert row["path_tags"] == ["6310-FC-3121", "6310-FC-3122"]
    assert {"6310-FC-3121", "6310-FC-3122"} <= set(row["equipment_tags"])
    assert len(row["source_paths"]) == 2
    assert row["title"].startswith("a — Cell N°1 6310-FC-3121")
    single = build_row(
        _chunk(), (0.0,) * 3, placements=placements[:1], family="pdf", settings=settings
    )
    assert single["projection_id"] == row["projection_id"]
    assert single["payload_sha256"] != row["payload_sha256"]


def test_schema_references_resolve_and_fit_agentic_retrieval() -> None:
    schema = index_schema()
    fields = _fields(schema)
    analyzers = {item["name"] for item in schema["analyzers"]}
    tokenizers = {item["name"] for item in schema["tokenizers"]}
    assert [name for name, item in fields.items() if item.get("key")] == ["projection_id"]
    assert len(schema["description"]) <= 4000
    for analyzer in schema["analyzers"]:
        assert analyzer["tokenizer"] in tokenizers | PREDEFINED_TOKENIZERS
    for item in fields.values():
        if "analyzer" in item:
            assert item["analyzer"] in PREDEFINED_ANALYZERS | analyzers
        for pair in ("indexAnalyzer", "searchAnalyzer"):
            if pair in item:
                assert item[pair] in analyzers, "analyzer pairs must be custom analyzers"
        if "normalizer" in item:
            assert item.get("filterable") or item.get("facetable")
    vector = fields["content_vector"]
    assert vector["dimensions"] == EmbeddingSettings().dimensions
    assert vector["retrievable"] is False
    assert vector["stored"] is False
    search = schema["vectorSearch"]
    profile = next(p for p in search["profiles"] if p["name"] == vector["vectorSearchProfile"])
    assert profile["algorithm"] in {a["name"] for a in search["algorithms"]}
    assert profile["compression"] in {c["name"] for c in search["compressions"]}
    assert profile["vectorizer"] in {v["name"] for v in search["vectorizers"]}
    semantic = schema["semantic"]
    config = next(
        c for c in semantic["configurations"] if c["name"] == semantic["defaultConfiguration"]
    )
    prioritized = config["prioritizedFields"]
    used = [
        prioritized["titleField"]["fieldName"],
        *(f["fieldName"] for f in prioritized["prioritizedContentFields"]),
        *(f["fieldName"] for f in prioritized["prioritizedKeywordsFields"]),
    ]
    for name in used:
        assert fields[name]["searchable"], name
        assert fields[name]["retrievable"], name
    placeholders = set(re.findall(r"\$\{(\w+)\}", json.dumps(schema)))
    assert placeholders == {"EMBEDDING_RESOURCE_URI", "QUERY_EMBEDDING_DEPLOYMENT"}


def test_identifier_query_tokenizer_keeps_codes_whole() -> None:
    pattern = index_schema()["tokenizers"][0]["pattern"]
    text = "Voir 6520-CV-5237, SP0904-M023.00-G02-0013. (B39) «PA1»"

    tokens = [token for token in re.split(pattern, text) if token]

    assert tokens == ["Voir", "6520-CV-5237", "SP0904-M023.00-G02-0013", "B39", "PA1"]


def test_knowledge_source_reads_only_schema_fields_and_never_access_fields() -> None:
    schema = index_schema()
    fields = _fields(schema)
    source = json.loads(repo_file(KNOWLEDGE_SOURCE).read_text(encoding="utf-8"))
    parameters = source["searchIndexParameters"]

    assert parameters["searchIndexName"] == schema["name"]
    assert parameters["semanticConfigurationName"] == schema["semantic"]["defaultConfiguration"]
    for item in parameters["searchFields"]:
        assert fields[item["name"]]["searchable"], item
    returned = {item["name"] for item in parameters["sourceDataFields"]}
    assert all(fields[name]["retrievable"] for name in returned)
    assert not returned & ACCESS_FIELDS
    assert "release_id eq '${RELEASE_ID}'" in parameters["baseFilter"]
    assert "access_scope_id ne 'unassigned'" in parameters["baseFilter"]


def test_knowledge_source_query_hints_use_indexed_values_within_service_limits() -> None:
    fields = _fields(index_schema())
    source = json.loads(repo_file(KNOWLEDGE_SOURCE).read_text(encoding="utf-8"))
    hints = source["searchIndexParameters"]["queryHints"]["filters"]
    by_field = {hint["field"]: hint["fieldValues"] for hint in hints}

    assert len(hints) <= 5
    assert len(by_field) == len(hints)
    for hint in hints:
        assert fields[hint["field"]]["filterable"], hint["field"]
        assert all(len(value) <= 128 for value in hint["fieldValues"])
        assert sum(len(value) for value in hint["fieldValues"]) <= 2048
        assert len(hint["filterInstructions"]) <= 1024
        assert not set(hint) & ACCESS_FIELDS
    assert set(by_field["document_class"]) == set(DOCUMENT_CLASSES) - {"unclassified"}
    for zone in by_field["zone_name"]:
        assert placement_metadata(f"1-{zone}/x.pdf").zone_name == zone


def test_backend_evidence_reader_fields_exist_in_the_schema() -> None:
    source = repo_file("src/backend/backend/tools/evidence_search.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    selected = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_SELECT" for t in node.targets)
    )
    filtered = {name for name in re.findall(r"[\"'](\w+)(?:/any\(| eq )", source) if len(name) > 1}
    fields = _fields(index_schema())

    assert {"release_id", "acl_groups", "equipment_tags"} <= filtered
    for name in selected:
        assert fields[name]["retrievable"], name
    for name in filtered:
        assert fields[name]["filterable"], name
    semantic = re.search(r'SEMANTIC_CONFIGURATION = "([^"]+)"', source)
    if semantic is None:
        pytest.fail("backend semantic configuration not found")
    assert semantic.group(1) == index_schema()["semantic"]["defaultConfiguration"]
