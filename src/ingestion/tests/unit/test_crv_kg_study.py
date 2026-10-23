"""KG-2 pilot extraction contract: grounding, value parsing, strict schema, SQL vocabulary sync."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from ingest_pipeline.domain.crv_kg.study import (
    Basis,
    DocumentExtraction,
    Locator,
    ObservationExtraction,
    Property,
    SampleRef,
    Stream,
    StudyExtraction,
    ground,
    normalize_text,
    openai_schema,
    parse_value,
    vocabulary,
)
from ingest_pipeline.domain.crv_kg.vocab import EDGE_LABELS, ENTITY_KINDS, METHODS

PAGES = {
    1: "Essais pilotes de flottation \u2013 Tizert\u00a0Master Composite 1 (114 kg), "
    "usine pilote CRV.",
    7: "Récupération Cu : 87,5 % ; concentré à 21,5 % Cu.\n13 360 t traitées en 2012.",
}
MIGRATION = (
    Path(__file__).resolve().parents[2] / "ingest_pipeline" / "sql" / "0020_crv_kg_study.sql"
)


def study(quote: str, page: int = 1, samples: list[SampleRef] | None = None) -> StudyExtraction:
    return StudyExtraction(
        label="Pilote Tizert",
        scale="pilot",
        period="2015",
        facility="CRV",
        commissioned_by=None,
        partners=[],
        commodities=["Cu"],
        processes=["flotation"],
        samples=samples or [],
        reagents=[],
        follows_or_validates=[],
        locator=Locator(page=page, quote=quote),
    )


def observation(value: str, quote: str, page: int = 7, study_index: int | None = 0) -> Any:
    return ObservationExtraction(
        study=study_index,
        sample=None,
        site="Tizert",
        property="recovery",
        element="Cu",
        stream="concentrate",
        basis=None,
        value_text=value,
        unit="%",
        qualifier=None,
        locator=Locator(page=page, quote=quote),
    )


def test_values_are_parsed_from_the_printed_text_never_from_the_model() -> None:
    assert parse_value("87,5 %") == Decimal("87.5")
    assert parse_value("13 360 t") == Decimal("13360")
    assert parse_value("≈ 43 %") == Decimal("43")
    assert parse_value("85-88 %") is None
    assert parse_value("élevée") is None
    assert normalize_text("Tizert\u00a0Master  \u2013\nComposite") == "Tizert Master - Composite"


def test_ground_keeps_only_items_stated_on_their_cited_page() -> None:
    good_sample = SampleRef(
        name="Tizert Master Composite 1",
        origin=None,
        material_kind="composite",
        mass_kg=114.0,
        locator=Locator(page=1, quote="Tizert Master Composite 1 (114 kg)"),
    )
    bad_sample = good_sample.model_copy(update={"locator": Locator(page=7, quote="114 kg")})
    extraction = DocumentExtraction(
        studies=[
            study("Ancien pilote 2010", page=1),
            study(
                "Essais pilotes de flottation - Tizert Master", samples=[good_sample, bad_sample]
            ),
        ],
        observations=[
            observation("87,5 %", "Récupération Cu : 87,5 %", study_index=1),
            observation("91 %", "Récupération Cu : 87,5 %", study_index=1),
            observation("13 360 t", "13 360 t traitées", study_index=None),
            observation("21,5 %", "concentré à 21,5 % Cu", page=1),
        ],
    )
    grounded = ground(extraction, PAGES)
    [kept] = grounded.studies
    assert [s.name for s in kept.samples] == ["Tizert Master Composite 1"]
    assert [
        (o.observation.value_text, o.observation.study, o.value_num) for o in grounded.observations
    ] == [
        ("87,5 %", 0, Decimal("87.5")),
        ("13 360 t", None, Decimal("13360")),
    ]
    assert grounded.rejected == [
        "study 0: quote not on page 1",
        "study 1: 1 sample/reagent quotes not on page",
        "observation 1: value not in its quote",
        "observation 3: quote not on page 1",
    ]


def test_openai_schema_is_strict_everywhere() -> None:
    schema = openai_schema(DocumentExtraction)

    def objects(node: Any) -> list[dict[str, Any]]:
        if isinstance(node, dict):
            found = [node] if node.get("type") == "object" and "properties" in node else []
            return found + [o for value in node.values() for o in objects(value)]
        if isinstance(node, list):
            return [o for item in node for o in objects(item)]
        return []

    assert len(objects(schema)) >= 6
    for node in objects(schema):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    assert '"default"' not in str(schema).replace("'", '"')


def _sql_list(column: str) -> set[str]:
    text = MIGRATION.read_text(encoding="utf-8")
    match = re.search(rf"\b{column} IN \((.*?)\)\)", text, re.S)
    assert match is not None, column
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_migration_vocabularies_match_the_python_contract() -> None:
    assert _sql_list("property") == set(vocabulary(Property))
    assert _sql_list("stream") == set(vocabulary(Stream))
    assert _sql_list("basis") == set(vocabulary(Basis))
    assert _sql_list("kind") == set(ENTITY_KINDS)
    assert _sql_list("predicate") == set(EDGE_LABELS)
    assert _sql_list("method") == set(METHODS)
