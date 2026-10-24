"""kg-extract import: scope per document, failed/excluded/unknown documents left out, no dups;
kg-import records exactly the reviewed file or nothing."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ingest_pipeline import cli
from ingest_pipeline.adapters.postgres import crv_kg_extract
from ingest_pipeline.domain.crv_kg.build import document_iri
from ingest_pipeline.domain.crv_kg.records import document_scope
from ingest_pipeline.domain.crv_kg.study import (
    DocumentExtraction,
    Grounding,
    Locator,
    ObservationExtraction,
    StudyDocument,
    ground,
)
from ingest_pipeline.domain.crv_kg.table_rules import TableObservation

ROOT = "AI for Science_ Geology & Chemistry"
DOSSIER = f"{ROOT}/Geometallurgy Cu/Week 22-06-2026/Dossier Cu"
TABLE = TableObservation(
    property="concentrate-grade", stream="concentrate", element="Cu", value_text="21,5 %",
    value=Decimal("21.5"), unit="%", qualifier=None, quote="Teneur Cu / Concentré: 21,5 %",
)


def test_document_scope_follows_the_collection_and_excludes_lims() -> None:
    assert document_scope(f"{DOSSIER}/Pilote Tizert.pdf") == "crv:geometallurgy"
    assert document_scope(f"{ROOT}/LIMS/Offre technique.pdf") is None


def document(digest: str, path: str) -> StudyDocument:
    return StudyDocument(
        content_hash=digest * 64, source_path=path, page_count=7,
        pages={7: "Récupération Cu : 87,5 %"}, tables=((7, TABLE), (7, TABLE)),
    )


class Connection:
    def __init__(self, held: set[str]) -> None:
        self.held = held

    async def fetch(self, query: str, iris: list[str]) -> list[dict[str, Any]]:
        return [{"iri": iri} for iri in iris if iri in self.held]


class Pool:
    def __init__(self, held: set[str]) -> None:
        self.held = held

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield Connection(self.held)


def test_import_records_only_extracted_documents_held_by_kg1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = ObservationExtraction(
        study=None, sample=None, site="Tizert", property="recovery", element="Cu", stream=None,
        basis=None, value_text="87,5 %", unit="%", qualifier=None,
        locator=Locator(page=7, quote="Récupération Cu : 87,5 %"),
    )
    kept, failed, lims, unknown = (
        document("a", f"{DOSSIER}/Pilote.pdf"), document("b", f"{DOSSIER}/Echec.pdf"),
        document("c", f"{ROOT}/LIMS/Offre technique.pdf"), document("d", f"{DOSSIER}/Neuf.pdf"),
    )
    grounding = ground(DocumentExtraction(studies=[], observations=[observation, observation]),
                       kept.pages)
    calls: list[tuple[list[str], Any]] = []

    async def fake_import(pool: Any, documents: list[str], records: Any, **_: Any) -> Any:
        calls.append((documents, records))
        return crv_kg_extract.ExtractImport("urn:crv:run:x", True, 0, 0, 0, 0)

    monkeypatch.setattr(crv_kg_extract, "import_extraction", fake_import)
    held = {document_iri(d.content_hash) for d in (kept, failed, lims)}
    report = asyncio.run(cli._import_extractions(
        Pool(held), [(kept, grounding), (failed, None), (lims, grounding), (unknown, grounding)],
        cli.datetime.now(cli.UTC),
    ))
    assert report["skipped"] == {"failed": 1, "excluded": 1, "not_in_kg1": 1}
    [(documents, records)] = calls
    assert documents == [document_iri(kept.content_hash)]
    assert sorted(o.method for o in records.observations) == ["llm", "table"]
    assert len({e.iri for e in records.evidence}) == len(records.evidence) == 2


OBSERVATION = ObservationExtraction(
    study=None, sample=None, site="Tizert", property="recovery", element="Cu", stream=None,
    basis=None, value_text="87,5 %", unit="%", qualifier=None,
    locator=Locator(page=7, quote="Récupération Cu : 87,5 %"),
)


def extract_line(document: StudyDocument, grounding: Grounding | None) -> str:
    """A kg-extract JSONL line (the fields kg-import reads)."""

    return json.dumps({
        "content_hash": document.content_hash, "source_path": document.source_path,
        "error": None if grounding else "extraction_failed",
        "studies": [s.model_dump(mode="json") for s in grounding.studies] if grounding else [],
        "observations": [
            {**o.observation.model_dump(mode="json"), "value_num": str(o.value_num)}
            for o in grounding.observations
        ] if grounding else [],
        "rejected": grounding.rejected if grounding else [],
    }, ensure_ascii=False)


def run_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: list[str],
    current: list[StudyDocument], *flags: str,
) -> tuple[int, list[list[tuple[StudyDocument, Grounding]]]]:
    imports: list[list[tuple[StudyDocument, Grounding]]] = []

    @asynccontextmanager
    async def runtime(*_: Any, **__: Any) -> Any:
        yield SimpleNamespace(pool=None, services=SimpleNamespace(require_artifacts=lambda: None))

    async def inputs(*_: Any, only: frozenset[str], tables: bool, **__: Any) -> Any:
        assert tables
        return [document for document in current if document.content_hash in only]

    async def record(_: Any, extracted: Any, __: Any) -> dict[str, Any]:
        imports.append(extracted)
        return {"documents": len(extracted)}

    monkeypatch.setattr(cli, "azure_runtime", runtime)
    monkeypatch.setattr(cli, "load_study_inputs", inputs)
    monkeypatch.setattr(cli, "_import_extractions", record)
    path = tmp_path / "reviewed.jsonl"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    args = cli.build_parser().parse_args(["kg-import", "--from", str(path), *flags])
    return asyncio.run(args.handler(None, None, args)), imports


def test_kg_import_records_exactly_the_reviewed_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    kept, failed = document("a", f"{DOSSIER}/Pilote.pdf"), document("b", f"{DOSSIER}/Echec.pdf")
    reviewed = ground(DocumentExtraction(studies=[], observations=[OBSERVATION]), kept.pages)
    lines = [extract_line(kept, reviewed), extract_line(failed, None)]
    code, imports = run_import(monkeypatch, tmp_path, lines, [kept, failed])
    assert code == 0
    [[(imported, grounding)]] = imports
    assert imported is kept
    assert grounding == reviewed


def test_kg_import_refuses_drift_and_dry_run_records_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    kept, gone = document("a", f"{DOSSIER}/Pilote.pdf"), document("c", f"{DOSSIER}/Ancien.pdf")
    reviewed = ground(DocumentExtraction(studies=[], observations=[OBSERVATION]), kept.pages)
    line = extract_line(kept, reviewed)
    assert run_import(monkeypatch, tmp_path, [line], [kept], "--dry-run") == (0, [])
    edited = replace(kept, pages={7: "Récupération Cu : 78,5 %"})
    assert run_import(monkeypatch, tmp_path, [line], [edited]) == (2, [])
    stale = [line, extract_line(gone, reviewed)]
    assert run_import(monkeypatch, tmp_path, stale, [kept]) == (2, [])
