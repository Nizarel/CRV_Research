"""KG-2 extraction import on PostgreSQL 16 + AGE: scoped supersession, KG-1 left untouched."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from integration.test_crv_kg_store import NOW, _import, store  # noqa: F401  (fixture)

from ingest_pipeline.adapters.postgres.crv_kg import CrvKgStore
from ingest_pipeline.adapters.postgres.crv_kg_extract import import_extraction
from ingest_pipeline.domain.crv_kg.build import document_iri
from ingest_pipeline.domain.crv_kg.records import extraction_records
from ingest_pipeline.domain.crv_kg.study import (
    DocumentExtraction,
    Locator,
    ObservationExtraction,
    StudyExtraction,
    ground,
)

pytestmark = pytest.mark.integration

HASH = "a" * 64
PAGES = {1: "Pilotage du Far West de Bleida", 5: "Récupération Cu : 87,5 % ; 91 %"}


def _records(values: tuple[str, ...]) -> Any:
    study = StudyExtraction(
        label="Pilotage Far West", scale="pilot", period=None, facility=None,
        commissioned_by=None, partners=[], commodities=["Cu"], processes=["flotation"],
        samples=[], reagents=[], follows_or_validates=[],
        locator=Locator(page=1, quote="Pilotage du Far West de Bleida"),
    )
    observations = [
        ObservationExtraction(
            study=0, sample=None, site="Bleida", property="recovery", element="Cu", stream=None,
            basis=None, value_text=value, unit="%", qualifier=None,
            locator=Locator(page=5, quote="Récupération Cu : 87,5 % ; 91 %"),
        )
        for value in values
    ]
    grounding = ground(DocumentExtraction(studies=[study], observations=observations), PAGES)
    return extraction_records(HASH, "BLEIDA/Pilotage.pdf", "crv:research", grounding)


async def _statuses(pg_pool: Any) -> dict[str, str]:
    async with pg_pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT item.value_text, state.status FROM crv_kg.observation AS item"
            " JOIN crv_kg.observation_current AS state ON state.observation_iri = item.iri"
        )
    return {row["value_text"]: row["status"] for row in rows}


async def test_extraction_import_is_idempotent_scoped_and_kg1_safe(
    store: CrvKgStore, pg_pool: Any,  # noqa: F811
) -> None:
    await _import(store, "abcd")
    documents = [document_iri(HASH)]
    first = await import_extraction(pg_pool, documents, _records(("87,5 %", "91 %")),
                                    started_at=NOW, now=NOW)
    assert (first.run_created, first.entities, first.observations) == (True, 1, 2)
    again = await import_extraction(pg_pool, documents, _records(("87,5 %", "91 %")),
                                    started_at=NOW, now=NOW)
    assert not again.run_created
    later = NOW + timedelta(hours=1)
    second = await import_extraction(pg_pool, documents, _records(("87,5 %",)),
                                     started_at=later, now=later)
    assert second.superseded == 1
    assert await _statuses(pg_pool) == {"87,5 %": "candidate", "91 %": "superseded"}
    async with pg_pool.acquire() as connection:
        value = await connection.fetchval(
            "SELECT value_num FROM crv_kg.observation WHERE value_text = '87,5 %'"
        )
        link = await connection.fetchval(
            "SELECT state.status FROM crv_kg.assertion AS link JOIN crv_kg.assertion_current"
            " AS state ON state.assertion_iri = link.iri WHERE link.predicate = 'reports'"
        )
    assert (value, link) == (Decimal("87.5"), "candidate")
    # A KG-1 rebuild must not supersede the extraction records it does not report.
    rebuilt = await _import(store, "abcd", NOW + timedelta(hours=2))
    assert rebuilt.superseded_entities == 0
    assert rebuilt.superseded_assertions == 0
    projection = await store.current_projection(statuses=("candidate",))
    assert f"urn:crv:study:{HASH}/0" in {vertex.iri for vertex in projection.vertices}


async def test_released_studies_and_observations_are_readable_by_scope(
    store: CrvKgStore, pg_pool: Any,  # noqa: F811
) -> None:
    await _import(store, "abcd")
    await import_extraction(pg_pool, [document_iri(HASH)], _records(("87,5 %",)),
                            started_at=NOW, now=NOW)
    release = (await store.build_release(
        statuses=("approved", "candidate", "observed"), started_at=NOW, now=NOW,
    )).release_id
    bleida = ["urn:crv:site:bleida"]
    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE crv_kg_reader")
        studies = await connection.fetch(
            "SELECT * FROM crv_kg.find_studies($1, $2, $3)", release, ["crv:research"], bleida,
        )
        values = await connection.fetch(
            "SELECT * FROM crv_kg.get_observations($1, $2, p_properties => $3)",
            release, ["crv:research"], ["recovery"],
        )
        hidden = await connection.fetch(
            "SELECT * FROM crv_kg.get_observations($1, $2)", release, ["crv:environment"],
        )
    [study] = studies
    assert (study["study_iri"], study["scale"], study["content_hash"], study["pages"]) == (
        f"urn:crv:study:{HASH}/0", "pilot", HASH, [1],
    )
    [value] = values
    assert (value["value_num"], value["page"], value["status"], value["study_label"]) == (
        Decimal("87.5"), 5, "candidate", "Pilotage Far West",
    )
    assert value["quote"] == "Récupération Cu : 87,5 % ; 91 %"
    assert hidden == []
