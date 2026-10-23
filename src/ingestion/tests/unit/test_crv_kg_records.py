"""KG-2 grounded extractions as deterministic candidate records."""

from __future__ import annotations

from decimal import Decimal

from ingest_pipeline.domain.crv_kg.build import document_iri
from ingest_pipeline.domain.crv_kg.records import ExtractRecords, extraction_records
from ingest_pipeline.domain.crv_kg.study import (
    DocumentExtraction,
    Locator,
    ObservationExtraction,
    SampleRef,
    StudyExtraction,
    ground,
)
from ingest_pipeline.domain.crv_kg.table_rules import TableObservation

HASH = "b" * 64
PAGES = {1: "Pilote Tizert 2015 sur Tizert Master Composite 1", 7: "Récupération Cu : 87,5 %"}
SAMPLE = SampleRef(name="Tizert Master Composite 1", origin=None, material_kind="composite",
                   mass_kg=None, locator=Locator(page=1, quote="Tizert Master Composite 1"))
STUDY = StudyExtraction(
    label="Pilote Tizert", scale="pilot", period="2015", facility=None, commissioned_by=None,
    partners=[], commodities=["Cu"], processes=["flotation"], samples=[SAMPLE], reagents=[],
    follows_or_validates=[], locator=Locator(page=1, quote="Pilote Tizert 2015"),
)
OBSERVATION = ObservationExtraction(
    study=0, sample="Tizert Master Composite 1", site=None, property="recovery", element="Cu",
    stream=None, basis=None, value_text="87,5 %", unit="%", qualifier=None,
    locator=Locator(page=7, quote="Récupération Cu : 87,5 %"),
)
TABLE = TableObservation(
    property="concentrate-grade", stream="concentrate", element="Cu", value_text="21,5 %",
    value=Decimal("21.5"), unit="%", qualifier=None, quote="Teneur Cu (%) / Concentré: 21,5 %",
)


def records() -> ExtractRecords:
    grounding = ground(DocumentExtraction(studies=[STUDY], observations=[OBSERVATION]), PAGES)
    return extraction_records(HASH, "Dossier Cu/Pilote.pdf", "crv:geometallurgy", grounding,
                              [(9, TABLE)])


def test_records_link_document_study_sample_and_observations_as_candidates() -> None:
    built = records()
    study, sample = built.entities
    assert (study.iri, study.kind, study.status) == (
        f"urn:crv:study:{HASH}/0", "Study", "candidate",
    )
    assert sample.iri == f"urn:crv:sample:{HASH}/0/tizert-master-composite-1"
    assert [(a.subject, a.predicate, a.object) for a in built.assertions] == [
        (document_iri(HASH), "reports", study.iri), (study.iri, "uses", sample.iri),
    ]
    llm, table = built.observations
    assert (llm.study_iri, llm.sample_iri, llm.value_num, llm.method) == (
        study.iri, sample.iri, Decimal("87.5"), "llm",
    )
    assert (table.property, table.method, table.study_iri) == ("concentrate-grade", "table", None)
    assert {e.locator["page"] for e in built.evidence} == {1, 7, 9}
    assert all(e.scope_key == "crv:geometallurgy" for e in built.evidence)


def test_records_are_deterministic_across_runs() -> None:
    first, second = records(), records()
    assert [o.iri for o in first.observations] == [o.iri for o in second.observations]
    assert [e.iri for e in first.evidence] == [e.iri for e in second.evidence]
