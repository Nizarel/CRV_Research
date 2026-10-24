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


def test_masses_and_grades_are_not_read_as_yields_or_recoveries() -> None:
    assert table_observation(cell("1,1", ("Poids (Kg)",), "TIZ RS 40 SB", None)) is None
    assert table_observation(cell("39.5", ("POIDS", "gr"), "Cr Sulf 2", None)) is None
    assert table_observation(cell("14,90", ("Au", "Alim. Rec (ppm)"), "Essai 2", None)) is None
    weight = table_observation(cell("5,75", ("Rdt Poids (%)",), "Ebauchage Oxydes", None))
    assert weight is not None
    assert (weight.property, weight.unit, weight.element) == ("weight-yield", "%", None)
    silver = table_observation(cell(
        "190", ("Teneurs concentré (% ou ppm)", "Ag (ppm)"), "Concentré final", None,
    ))
    assert silver is not None
    assert (silver.property, silver.element, silver.unit) == ("concentrate-grade", "Ag", "ppm")


def test_hours_moisture_and_test_conditions_are_not_measurements_or_streams() -> None:
    tonnage = "Tonnage Tout venant traité"
    assert table_observation(cell("8", (tonnage, "Heures de marche"), "05-avr", None)) is None
    assert table_observation(cell("4,24", (tonnage, "H%"), "05-avr", None)) is None
    wet = table_observation(cell("711", (tonnage, "T humide"), "05-avr", None))
    assert wet is not None
    assert wet.property == "tonnage"
    condition = table_observation(cell(
        "80,75", ("Récupération (%)", "Cu"),
        "- Prolongation de temps de séjour du circuit oxydé (Ajout d'un épuisement)", "%",
    ))
    assert condition is not None
    assert condition.stream is None
    feed = table_observation(cell("100", ("Distribution (%)", "As (%)"), "Alim. Reconsituée"))
    assert feed is not None
    assert feed.stream == "feed"


def test_elements_come_from_headers_or_symbols_and_units_must_be_unambiguous() -> None:
    bismuth = table_observation(cell(
        "22", ("Teneurs concentré (% ou ppm)", "Bi (ppm)"), "RC 172 SB", None,
    ))
    assert bismuth is not None
    assert (bismuth.element, bismuth.unit) == ("Bi", "ppm")
    assert table_observation(cell("1,1", ("Distribution (%)",), "RC 211 SB", None)) is None
    antimony = table_observation(cell("1,1", ("Distribution (%)",), "Sb", None))
    assert antimony is not None
    assert antimony.element == "Sb"
    oxide = table_observation(cell("1,78", ("Cu Ox", "Teneur (%)"), "Alim. Rec.", None))
    assert oxide is not None
    assert (oxide.property, oxide.element) == ("head-grade", "CuOx")
    assert table_observation(cell("29", ("Teneur (%) ou ppm", "Ag"), "Alim. Recon.", None)) is None


def test_condition_headers_and_english_concentrates() -> None:
    grade = table_observation(cell(
        "23,36", ("D80 = 45 µm", "Analyses", "Cu (%)"), "Conc. Relavage 4", None,
    ))
    assert grade is not None
    assert (grade.property, grade.element, grade.stream, grade.unit) == (
        "concentrate-grade", "Cu", "cleaner-concentrate", "%",
    )
    assert grade.quote.startswith("D80 = 45 µm / Analyses")
    assert table_observation(cell("45", ("D80 = 45 µm",), "Conc. Relavage 4", None)) is None
    silver = table_observation(cell(
        "6.5", ("Distribution - percent", "Ag"), "Copper Sul Con I", None,
    ))
    assert silver is not None
    assert (silver.property, silver.element, silver.stream, silver.unit) == (
        "distribution", "Ag", "concentrate", "%",
    )
