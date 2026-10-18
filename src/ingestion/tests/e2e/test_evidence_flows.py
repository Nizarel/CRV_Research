"""End-to-end evidence flows over the in-memory harness with real PDF/XLSX parsers and fake DI."""

from __future__ import annotations

from pathlib import Path

import pytest
from evidence_fakes import FakeLayout, permanent, transient
from evidence_harness import EvidenceKit, build_evidence_harness
from fixture_builders import A3_LANDSCAPE, PdfPage, Sheet, pdf_bytes, xlsx_bytes

from tizertmaint_pipeline.domain.evidence import EvidenceDocument, PageStatus
from tizertmaint_pipeline.domain.models import SourceSnapshot, UnitDisposition
from tizertmaint_pipeline.domain.projection import ProjectionState
from tizertmaint_pipeline.domain.routing import Route, RoutingPolicy
from tizertmaint_pipeline.domain.states import JobStatus, StageName
from tizertmaint_pipeline.ports.evidence import EvidenceStatus, FigureState
from tizertmaint_pipeline.stages.chunkers import ChunkingSettings
from tizertmaint_pipeline.stages.di import DiSettings
from tizertmaint_pipeline.stages.plan import LayoutOptions

PROSE = [
    "Le broyeur primaire BR-101 est alimente par le convoyeur CV-12.",
    "La pompe de circulation P-201 assure le debit nominal de 45 m3/h.",
    "Verifier le serrage des boulons de fixation chaque semaine.",
]


def text_page(*extra: str) -> PdfPage:
    return PdfPage(lines=[*PROSE, *extra])


async def _manifest(kit: EvidenceKit, content_hash: str) -> EvidenceDocument:
    record = await kit.store.latest_document(
        content_hash, kit.policy.version, status=EvidenceStatus.EMBEDDED
    )
    assert record is not None
    data = await kit.harness.artifacts.get(record.manifest)
    return EvidenceDocument.model_validate_json(data)


def _hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def kit(tmp_path: Path) -> EvidenceKit:
    return build_evidence_harness(tmp_path)


async def test_native_pdf_flows_to_verified_rows(kit: EvidenceKit) -> None:
    data = pdf_bytes([text_page("Page un."), text_page("Page deux.")], labels=["i", "ii"])
    await kit.ingest("Broyage/notice.pdf", data)
    await kit.drain()

    assert kit.statuses(StageName.NATIVE_PARSE) == [JobStatus.SUCCEEDED]
    assert not kit.jobs(StageName.DI_SUBMIT)
    assert kit.statuses(StageName.PROJECT_SEARCH) == [JobStatus.SUCCEEDED] * 2
    rows = list(kit.index.rows.values())
    assert rows
    assert all(row["original_page_numbers"] for row in rows)
    assert {page for row in rows for page in row["original_page_numbers"]} == {1, 2}
    assert all(row["access_scope_id"] == "unassigned" for row in rows)
    items = list(kit.store.projections.values())
    assert {item.state for item in items} == {ProjectionState.VERIFIED}
    manifest = await _manifest(kit, _hash(data))
    assert manifest.complete
    assert [page.status for page in manifest.pages] == [PageStatus.CONTENT] * 2


async def test_duplicate_placements_share_one_extraction_chain_and_one_row_set(
    kit: EvidenceKit,
) -> None:
    data = pdf_bytes([text_page()])
    await kit.ingest("Broyage/a.pdf", data)
    await kit.ingest("Archive/copie-a.pdf", data)
    await kit.drain()

    assert len(kit.jobs(StageName.PREFLIGHT)) == 1
    assert len(kit.jobs(StageName.NATIVE_PARSE)) == 1
    assert len(kit.jobs(StageName.EMBED)) == 1
    assert sum(kit.embeddings.calls) == len(kit.store.vectors)
    rows = list(kit.index.rows.values())
    assert len(rows) == len({row["chunk_id"] for row in rows}) > 0
    assert all(len(row["placement_ids"]) == 2 for row in rows)
    assert all(row["source_uri"].endswith("Broyage/a.pdf") for row in rows)


async def test_a_copy_added_later_joins_the_rows_and_a_moved_copy_leaves_them(
    kit: EvidenceKit,
) -> None:
    data = pdf_bytes([text_page()])
    await kit.ingest("Broyage/a.pdf", data)
    await kit.drain()
    keys = set(kit.index.rows)

    await kit.ingest("Flottation/copie-a.pdf", data)
    await kit.drain()
    assert set(kit.index.rows) == keys
    assert all(len(row["placement_ids"]) == 2 for row in kit.index.rows.values())

    kit.harness.clock.advance(60)
    await kit.ingest("Flottation/copie-a.pdf", pdf_bytes([text_page("Autre document.")]))
    await kit.drain()
    original = [row for row in kit.index.rows.values() if row["content_hash"] == _hash(data)]
    assert {row["projection_id"] for row in original} == keys
    assert all(len(row["placement_ids"]) == 1 for row in original)
    assert all(row["zone_names"] == ["Broyage"] for row in original)
    assert len(kit.index.rows) > len(keys)


async def test_rows_of_a_content_no_longer_placed_anywhere_are_retired(kit: EvidenceKit) -> None:
    first = pdf_bytes([text_page("Version A.")])
    await kit.ingest("Broyage/a.pdf", first)
    await kit.ingest("Archive/a.pdf", first)
    await kit.drain()
    old_keys = set(kit.index.rows)

    second = pdf_bytes([text_page("Version B.")])
    kit.harness.clock.advance(60)
    await kit.ingest("Broyage/a.pdf", second)
    await kit.drain()
    assert old_keys <= set(kit.index.rows)

    kit.harness.clock.advance(60)
    await kit.ingest("Archive/a.pdf", second)
    await kit.drain()
    assert not old_keys & set(kit.index.rows)
    assert all(len(row["placement_ids"]) == 2 for row in kit.index.rows.values())


async def test_projection_runs_again_when_placements_change_mid_run(kit: EvidenceKit) -> None:
    await kit.ingest("Broyage/a.pdf", pdf_bytes([text_page()]))
    snapshots = kit.harness.snapshots
    for_content = snapshots.for_content

    async def racing(content_hash: str) -> list[SourceSnapshot]:
        # A copy lands after the rows were uploaded but before the job finishes.
        if kit.index.rows and len(snapshots.all()) == 1:
            [original] = snapshots.all()
            await snapshots.record(
                original.model_copy(
                    update={
                        "snapshot_id": "snap:copy",
                        "placement_id": "placement:copy",
                        "blob_name": "Archive/a.pdf",
                    }
                )
            )
        return await for_content(content_hash)

    snapshots.for_content = racing  # type: ignore[method-assign]
    await kit.drain()

    [job] = [j for j in kit.jobs(StageName.PROJECT_SEARCH) if "evidence_id" in j.inputs]
    outcomes = [a.outcome for a in await kit.harness.ledger.attempts(job.job_id)]
    assert outcomes == [JobStatus.RETRY_WAIT, JobStatus.SUCCEEDED]
    assert all(len(row["placement_ids"]) == 2 for row in kit.index.rows.values())


async def test_blank_and_drawing_pages_keep_original_indices(tmp_path: Path) -> None:
    kit = build_evidence_harness(tmp_path)
    data = pdf_bytes(
        [
            text_page(),
            PdfPage(),
            text_page(),
            PdfPage(size=A3_LANDSCAPE, vector_lines=12),
            PdfPage(image=True),
        ]
    )
    await kit.ingest("PFD/plan.pdf", data)
    await kit.drain()

    plan_jobs = kit.jobs(StageName.PLAN)
    assert [job.status for job in plan_jobs] == [JobStatus.SUCCEEDED]
    requests = [request for request, _ in kit.layout.submissions]
    assert sorted(request.pages or "" for request in requests) == ["4", "5"]
    manifest = await _manifest(kit, _hash(data))
    statuses = {page.page: page.status for page in manifest.pages}
    assert statuses[2] is PageStatus.BLANK
    assert set(statuses) == {1, 2, 3, 4, 5}
    routes = {unit.route for unit in manifest.units}
    assert {Route.NATIVE_PDF_TEXT.value, Route.DI_LAYOUT_DRAWING.value, Route.BLANK.value} <= routes
    drawing_rows = [
        row for row in kit.index.rows.values() if row["original_page_numbers"] == [4]
    ]
    assert drawing_rows
    assert all("sheet p4" in row["content"] for row in drawing_rows)


async def test_figures_are_captured_after_raw_and_failures_stay_isolated(tmp_path: Path) -> None:
    layout = FakeLayout(figure_faults={"1.1": [transient()], "2.1": [permanent()]})
    kit = build_evidence_harness(tmp_path, layout=layout)
    data = pdf_bytes([PdfPage(image=True), PdfPage(image=True), PdfPage(image=True)])
    await kit.ingest("Scans/scan.pdf", data)
    await kit.drain()

    figure_jobs = kit.jobs(StageName.FIGURE_ASSETS)
    assert [job.status for job in figure_jobs] == [JobStatus.SUCCEEDED]
    assert layout.figure_calls.count("1.1") == 2
    assert layout.figure_calls.count("3.1") == 1
    assets = {asset.figure_id: asset.state for asset in kit.store.figure_assets.values()}
    assert assets == {
        "1.1": FigureState.STORED,
        "2.1": FigureState.FAILED,
        "3.1": FigureState.STORED,
    }
    assert layout.deleted
    assert kit.index.rows


async def test_spreadsheet_keeps_original_cells_and_hidden_sheets(kit: EvidenceKit) -> None:
    data = xlsx_bytes(
        [
            Sheet(
                "Equipements",
                {
                    "B2": "Tag",
                    "C2": "Description",
                    "D2": "Debit",
                    "B3": "P-201",
                    "C3": "Pompe de circulation",
                    "D3": ("=40+5", 45),
                    "B4": "BR-101",
                    "C4": "Broyeur primaire",
                    "D4": 0,
                },
            ),
            Sheet("Cache", {"A1": "Note", "A2": "Interne"}, state="hidden"),
        ]
    )
    await kit.ingest("Registre/equipements.xlsx", data)
    await kit.drain()

    rows = list(kit.index.rows.values())
    assert rows
    assert all(row["original_page_numbers"] == [] for row in rows)
    locators = {locator for row in rows for locator in row["source_locators"]}
    assert "cell:Equipements!B2:D4" in locators
    assert "cell:Cache!A1:A2" in locators
    content = "\n".join(row["content"] for row in rows)
    assert "| P-201 | Pompe de circulation | 45 |" in content
    assert "| BR-101 | Broyeur primaire | 0 |" in content


async def test_failed_unit_makes_document_partial_and_needs_review(tmp_path: Path) -> None:
    from tizertmaint_pipeline.ports.providers import LayoutStatus

    layout = FakeLayout(fail_status=LayoutStatus.FAILED)
    kit = build_evidence_harness(tmp_path, layout=layout)
    data = pdf_bytes([text_page(), PdfPage(image=True)])
    await kit.ingest("Scans/mixte.pdf", data)
    await kit.drain()

    assert kit.statuses(StageName.DI_POLL) == [JobStatus.FAILED_PERMANENT]
    assert kit.statuses(StageName.CHUNK) == [JobStatus.NEEDS_REVIEW]
    assert not kit.index.rows
    plan = kit.jobs(StageName.PLAN)[0]
    units = await kit.harness.ledger.plan_units(plan.checkpoint["plan_id"])
    assert [state.disposition for state in units] == [
        UnitDisposition.EVIDENCE,
        UnitDisposition.FAILED,
    ]


async def test_partial_projection_when_allowed(tmp_path: Path) -> None:
    from tizertmaint_pipeline.ports.providers import LayoutStatus

    kit = build_evidence_harness(
        tmp_path,
        layout=FakeLayout(fail_status=LayoutStatus.FAILED),
        chunking=ChunkingSettings(allow_partial_projection=True),
    )
    data = pdf_bytes([text_page(), PdfPage(image=True)])
    await kit.ingest("Scans/mixte.pdf", data)
    await kit.drain()

    manifest = await _manifest(kit, _hash(data))
    assert manifest.completeness == "partial"
    assert {page.page: page.status for page in manifest.pages}[2] is PageStatus.MISSING
    assert kit.index.rows


async def test_admission_limits_in_flight_di_operations(tmp_path: Path) -> None:
    policy = RoutingPolicy(max_layout_pages_per_unit=1)
    kit = build_evidence_harness(
        tmp_path,
        policy=policy,
        di=DiSettings(max_inflight=1, admission_wait_seconds=5),
        layout=FakeLayout(polls_before_done=2),
    )
    data = pdf_bytes([PdfPage(image=True), PdfPage(image=True), PdfPage(image=True)])
    await kit.ingest("Scans/trois.pdf", data)
    await kit.drain(max_rounds=400)

    assert kit.statuses(StageName.DI_SUBMIT) == [JobStatus.SUCCEEDED] * 3
    waits = [
        attempt
        for job in kit.jobs(StageName.DI_SUBMIT)
        for attempt in await kit.harness.ledger.attempts(job.job_id)
        if attempt.outcome is JobStatus.RETRY_WAIT
    ]
    assert waits
    assert len(kit.layout.submissions) == 3


async def test_page_budget_blocks_submission(tmp_path: Path) -> None:
    kit = build_evidence_harness(
        tmp_path, di=DiSettings(page_budget=0, admission_wait_seconds=5)
    )
    data = pdf_bytes([PdfPage(image=True)])
    await kit.ingest("Scans/un.pdf", data)
    await kit.drain(max_rounds=10, strict=False)

    assert not kit.layout.submissions
    assert kit.statuses(StageName.DI_SUBMIT) == [JobStatus.RETRY_WAIT]


async def test_changed_content_retires_the_placements_old_rows(kit: EvidenceKit) -> None:
    first = pdf_bytes([text_page("Version A.")])
    await kit.ingest("Broyage/notice.pdf", first)
    await kit.drain()
    old_keys = set(kit.index.rows)
    assert old_keys

    second = pdf_bytes([text_page("Version B, revisee.")])
    await kit.ingest("Broyage/notice.pdf", second)
    await kit.drain()

    assert not old_keys & set(kit.index.rows)
    assert kit.index.rows
    assert all(row["content_hash"] == _hash(second) for row in kit.index.rows.values())
    retired = [i for i in kit.store.projections.values() if i.state is ProjectionState.RETIRED]
    assert {item.projection_id for item in retired} == old_keys


async def test_projection_waits_for_search_visibility(kit: EvidenceKit) -> None:
    kit.index.lagging_verifications = 2
    await kit.ingest("Broyage/notice.pdf", pdf_bytes([text_page()]))
    await kit.drain()

    [job] = [j for j in kit.jobs(StageName.PROJECT_SEARCH) if "evidence_id" in j.inputs]
    assert job.status is JobStatus.SUCCEEDED
    outcomes = [a.outcome for a in await kit.harness.ledger.attempts(job.job_id)]
    assert outcomes.count(JobStatus.RETRY_WAIT) == 2
    assert {i.state for i in kit.store.projections.values()} == {ProjectionState.VERIFIED}
    assert kit.index.uploads == 1


async def test_rejected_rows_need_review(kit: EvidenceKit) -> None:
    kit.index.reject_all = True
    await kit.ingest("Broyage/notice.pdf", pdf_bytes([text_page()]))
    await kit.drain()

    [job] = [j for j in kit.jobs(StageName.PROJECT_SEARCH) if "evidence_id" in j.inputs]
    assert job.status is JobStatus.NEEDS_REVIEW
    states = {item.state for item in kit.store.projections.values()}
    assert states == {ProjectionState.FAILED_PERMANENT}


async def test_large_sources_submit_subsets_with_original_page_numbers(tmp_path: Path) -> None:
    data = pdf_bytes([text_page(), PdfPage(image=True), PdfPage(image=True)])
    limit = len(data) - 1
    kit = build_evidence_harness(tmp_path, policy=RoutingPolicy(max_submit_bytes=limit))
    await kit.ingest("Scans/gros.pdf", data)
    await kit.drain()

    [(request, size)] = kit.layout.submissions
    assert request.pages is None
    assert size <= limit
    pages = {page for row in kit.index.rows.values() for page in row["original_page_numbers"]}
    assert pages == {1, 2, 3}


async def test_layout_options_change_extraction_fingerprints(tmp_path: Path) -> None:
    data = pdf_bytes([PdfPage(image=True)])
    fingerprints = []
    high_resolution = LayoutOptions(features=("ocrHighResolution",))
    for options in (LayoutOptions(), LayoutOptions(), high_resolution):
        kit = build_evidence_harness(tmp_path, layout_options=options)
        await kit.ingest("Scans/un.pdf", data)
        await kit.drain()
        [job] = kit.jobs(StageName.DI_SUBMIT)
        fingerprints.append(job.fingerprint)
    assert fingerprints[0] == fingerprints[1] != fingerprints[2]


async def test_unsupported_and_empty_sources_stop_after_planning(kit: EvidenceKit) -> None:
    await kit.ingest("Divers/archive.zip", _zip())
    await kit.ingest("Divers/vide.txt", b"")
    await kit.drain()

    assert kit.statuses(StageName.PLAN) == [JobStatus.SUCCEEDED] * 2
    assert not kit.jobs(StageName.CHUNK)
    assert not kit.index.rows


IFC_MODEL = (
    "ISO-10303-21;\nHEADER;\n"
    "FILE_NAME('6032-ZA-1110.ifc','2024-02-14T09:54:33',(''),('CMIMAROC'),'','Tekla 2021','');\n"
    "FILE_SCHEMA(('IFC2X3'));\nENDSEC;\nDATA;\n"
    "#10=IFCPROJECT('p',#2,'6032-ZA-1110','','','Bin Apron Feeder Discharge Chute','',(#3),#4);\n"
    "#20=IFCCARTESIANPOINT((0.,0.,0.));\n"
    "#30=IFCPLATE('a',#2,'PLATE',$,'PL10',#5,#6,'P1');\n"
    "ENDSEC;\nEND-ISO-10303-21;\n"
)


async def test_ifc_models_are_summarized_and_cad_is_lineage_only(kit: EvidenceKit) -> None:
    await kit.ingest("Stockage/6032-ZA-1110/1-Plans/6032-ZA-1110.ifc", IFC_MODEL.encode("latin-1"))
    await kit.ingest("Stockage/6032-ZA-1110/1-Plans/plate.dxf", b"  0\nSECTION\n  2\nENTITIES\n")
    await kit.drain()

    assert kit.statuses(StageName.PLAN) == [JobStatus.SUCCEEDED] * 2
    assert kit.statuses(StageName.NATIVE_PARSE) == [JobStatus.SUCCEEDED]
    rows = list(kit.index.rows.values())
    assert rows
    assert all(row["source_uri"].endswith(".ifc") for row in rows)
    content = "\n".join(row["content"] for row in rows)
    assert "6032-ZA-1110" in content
    assert "Bin Apron Feeder Discharge Chute" in content
    assert "0.,0.,0." not in content
    assert any(loc.startswith("lines:") for row in rows for loc in row["source_locators"])


def _dwg_kit(tmp_path: Path, mode: str) -> EvidenceKit:
    import sys

    from fixture_builders import FAKE_DWG2DXF

    from tizertmaint_pipeline.adapters.parsers.libredwg import LibreDwgReader

    reader = LibreDwgReader(
        command=(sys.executable, str(FAKE_DWG2DXF), mode), resource_limits=False
    )
    return build_evidence_harness(tmp_path, drawings=reader)


DWG_PATH = "Stockage/SP0904-0000-1S18-007 6520-CV-5237 Rev03/DWG/[S] - B39 (A3) 01-02.dwg"


async def test_dwg_shop_drawings_are_indexed_without_frame_text(tmp_path: Path) -> None:
    from fixture_builders import DWG_BYTES, SHOP_LAYOUT, shop_drawing_dxf

    dxf = shop_drawing_dxf(tmp_path / "prepared.dxf")
    kit = _dwg_kit(tmp_path, f"copy:{dxf}")
    await kit.ingest(DWG_PATH, DWG_BYTES)
    await kit.drain()

    assert kit.statuses(StageName.NATIVE_PARSE) == [JobStatus.SUCCEEDED]
    rows = list(kit.index.rows.values())
    assert rows
    content = "\n".join(row["content"] for row in rows)
    assert "POS_NUM: B39" in content
    assert "B39 | 1 | IPE270 | 659 | S275JR | 23.79" in content
    assert "Greenstone" not in content
    assert all(row["original_page_numbers"] == [] for row in rows)
    locators = {loc for row in rows for loc in row["source_locators"]}
    assert f"sheet:{SHOP_LAYOUT}" in locators
    title_block = next(row for row in rows if "POS_NUM: B39" in row["content"])
    assert title_block["drawing_sheet_id"] == f"sheet:{SHOP_LAYOUT}"
    assert title_block["revision"] == "02"
    assert {"B39", "6520-CV-5237", "SP0904-0000-1S18-007"} <= set(title_block["identifiers"])
    assert title_block["document_code"] == "SP0904-0000-1S18-007"
    assert title_block["title"].startswith("[S] - B39 (A3) 01-02 — SP0904-0000-1S18-007")
    assert {
        (row["zone_name"], row["family"], row["extraction_route"], row["unit_kind"])
        for row in rows
    } == {("Stockage", "cad", "native_dwg", "cad_drawing")}


async def test_unreadable_dwg_stays_lineage_only_without_review(tmp_path: Path) -> None:
    from fixture_builders import DWG_BYTES

    kit = _dwg_kit(tmp_path, "fail")
    await kit.ingest(DWG_PATH, DWG_BYTES)
    await kit.drain()

    assert kit.statuses(StageName.NATIVE_PARSE) == [JobStatus.NO_EVIDENCE]
    assert kit.statuses(StageName.CHUNK) == [JobStatus.NO_EVIDENCE]
    assert not kit.index.rows
    plan = kit.jobs(StageName.PLAN)[0]
    [state] = await kit.harness.ledger.plan_units(plan.checkpoint["plan_id"])
    assert state.disposition is UnitDisposition.NO_EVIDENCE
    assert (state.unit.reason or "").startswith("lineage only: DWG conversion failed (exit 1)")


def _zip() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "contenu")
    return buffer.getvalue()
