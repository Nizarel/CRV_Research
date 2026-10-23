"""kg-extract import: scope per document, failed/excluded/unknown documents left out, no dups."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest

from ingest_pipeline import cli
from ingest_pipeline.adapters.postgres import crv_kg_extract
from ingest_pipeline.domain.crv_kg.build import document_iri
from ingest_pipeline.domain.crv_kg.records import document_scope
from ingest_pipeline.domain.crv_kg.study import (
    DocumentExtraction,
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
