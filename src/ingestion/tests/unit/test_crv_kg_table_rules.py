"""KG-2 table rules: typed observations only when headers say what a cell measures."""

from __future__ import annotations

from decimal import Decimal

from ingest_pipeline.domain.crv_kg.table_rules import table_observation
from ingest_pipeline.domain.evidence import Locator, LocatorKind
from ingest_pipeline.domain.observations import ScientificObservation


def cell(
    raw: str, headers: tuple[str, ...], row: str | None, unit: str | None = "%",
) -> ScientificObservation:
    return ScientificObservation(
        evidence_id="e1", unit_id="u1", table_row=2, table_column=1, raw_value=raw,
        value=Decimal(raw.rstrip(" %").replace(",", ".")), unit_text=unit,
        column_headers=headers, row_label=row,
        locators=(Locator(kind=LocatorKind.PAGE, page=7),),
    )


def test_mass_balance_cells_map_to_property_stream_and_element() -> None:
    grade = table_observation(cell("21,5 %", ("Teneur Cu (%)",), "Concentré final"))
    assert grade is not None
    assert (grade.property, grade.stream, grade.element) == (
        "concentrate-grade", "concentrate", "Cu",
    )
    assert grade.quote == "Teneur Cu (%) / Concentré final: 21,5 %"
    recovery = table_observation(cell("87,5", ("Récupération Cu",), "Concentré ébauchage", None))
    assert recovery is not None
    assert (recovery.property, recovery.stream, recovery.unit) == (
        "recovery", "rougher-concentrate", None,
    )
    head = table_observation(cell("1,52", ("Teneur", "Cu %"), "Alimentation"))
    assert head is not None
    assert (head.property, head.stream, head.element) == ("head-grade", "feed", "Cu")
    work = table_observation(cell("10,2", ("Work Index (kWh/t)",), "Sidi Ali", None))
    assert work is not None
    assert (work.property, work.element) == ("work-index", None)


def test_ambiguous_cells_are_skipped_not_guessed() -> None:
    assert table_observation(cell("12", ("Essai 3",), "Ligne 2")) is None
    assert table_observation(cell("12", (), "Concentré")) is None
    assert table_observation(cell("85", ("Récupération",), "Concentré")) is None  # no element
    assert table_observation(cell("2,1", ("Teneur Cu %",), "Essai 4")) is None  # no stream
