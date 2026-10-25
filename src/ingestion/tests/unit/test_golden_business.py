"""Lossless business CSV import, provenance and legacy compatibility."""

import csv
from pathlib import Path

import pytest

from ingest_pipeline.tools.golden.business import (
    HEADERS,
    BusinessQuestion,
    import_csv,
    write_import,
)
from ingest_pipeline.tools.golden.dataset import GoldenQuestion


def source(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    path = tmp_path / "business.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    return path


def test_import_preserves_verbatim_cells_and_multiline_provenance(tmp_path: Path) -> None:
    path = source(tmp_path, [(" Question, exacte\nligne 2 ", " 87,5 % ; attendu "), ("Q2", "R2")])
    questions, audit = import_csv(path)
    assert questions[0].question == " Question, exacte\nligne 2 "
    assert questions[0].expected == " 87,5 % ; attendu "
    assert [fact.text for fact in questions[0].key_facts] == ["87,5 %", "attendu"]
    assert questions[0].evaluation.line_start == 2
    assert questions[0].evaluation.line_end == 3
    assert questions[1].evaluation.line_start == 4
    assert [item.id for item in questions] == ["Q77", "Q78"]
    assert audit["questions"] == 2
    assert questions[0].evaluation.review_status == "proposed"
    assert GoldenQuestion.model_validate(questions[0].model_dump()).number == 77


@pytest.mark.parametrize("rows", [[("Q", "")], [("", "R")]])
def test_missing_cells_are_not_silently_dropped(
    tmp_path: Path, rows: list[tuple[str, str]]
) -> None:
    with pytest.raises(ValueError, match="Missing or extra"):
        import_csv(source(tmp_path, rows))


def test_behaviors_do_not_turn_external_answers_into_blanket_abstention(tmp_path: Path) -> None:
    path = source(
        tmp_path,
        [
            (
                "Q1",
                "Hors corpus : une explication clairement "
                "pr\u00e9sent\u00e9e comme externe est admise",
            ),
            ("Q2", "Pr\u00e9misse \u00e0 corriger : pas de r\u00e9sultat industriel"),
            ("Calculez la diff\u00e9rence", "32,23 - 3,57 = 28,66"),
        ],
    )
    questions, _ = import_csv(path)
    assert not questions[0].must_abstain
    assert questions[0].evaluation.external_knowledge == "allowed_with_disclosure"
    assert questions[1].evaluation.behavior == "correct_premise"
    assert questions[2].evaluation.tools.all_of == ("calculate",)


def test_duplicates_remain_visible_and_frozen_outputs_cannot_be_overwritten(tmp_path: Path) -> None:
    path = source(tmp_path, [("Q", "R"), ("Q", "Autre")])
    output, review = tmp_path / "set.jsonl", tmp_path / "review.csv"
    audit = write_import(path, output, review)
    assert audit["duplicate_questions"] == ["Q"]
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
    assert BusinessQuestion.model_validate_json(output.read_text().splitlines()[0]).id == "Q77"
    with pytest.raises(ValueError, match="overwrite"):
        write_import(path, output, review)


def test_blank_records_are_rejected_not_silently_renumbered(tmp_path: Path) -> None:
    path = source(tmp_path, [("Q", "R")])
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="Missing or extra"):
        import_csv(path)


def test_actual_stakeholder_csv_is_losslessly_importable() -> None:
    path = Path(__file__).resolve().parents[4] / "Docs" / "QA_REMINEX_EXTENSION.csv"
    if not path.exists():
        pytest.skip("Optional business reference is absent")
    questions, audit = import_csv(path)
    assert len(questions) == 466
    assert audit["duplicate_questions"] == []
    assert questions[-1].id == "Q542"
    assert audit["reference_review"] == ["Q129", "Q304"]
