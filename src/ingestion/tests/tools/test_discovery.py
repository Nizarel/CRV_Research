import json
from pathlib import Path

import pytest

from tizertmaint_pipeline.tools.discovery import discover, disposition, inventory, path_context


def test_duplicates_keep_placements_and_portable_ids(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    for tag in ("6210-ML-2175", "6220-ML-2242"):
        target = root / "2-Zone Broyage" / tag / "1-Plans" / "drawing.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"same content")
    before = list(inventory(root, "tizert-reviewed"))
    moved = tmp_path / "moved"
    root.rename(moved)
    after = list(inventory(moved, "tizert-reviewed"))
    assert before == after
    assert len({record["content_id"] for record in after}) == 1
    assert len({record["placement_id"] for record in after}) == 2
    summary = discover(moved, tmp_path / "report", "tizert-reviewed", 0)
    assert summary["distinct_contents"] == 1
    assert summary["redundant_placements_preserved"] == 1
    assert summary["complete"]
    sources = [
        json.loads(line) for line in (tmp_path / "report/sources.jsonl").read_text().splitlines()
    ]
    assert len(sources) == 2


def test_multitag_suffix_and_direct_files() -> None:
    context = path_context("17-Sample Cabinet_6220-BN-2277 & 6320-BN-3201/direct.pdf")
    assert context["primary_equipment_tag_candidate"] is None
    assert len(context["equipment_tag_candidates"]) == 2
    assert context["document_category_raw"] is None
    context = path_context("10-Electrical/6220-X-2230A/direct.pdf")
    assert context["equipment_tag_candidates"] == ["6220-X-2230A"]
    assert context["document_class_candidate"] == "unclassified"


@pytest.mark.parametrize(
    ("name", "size", "route"),
    [
        ("a.weird", 4, "unsupported_review"),
        ("a.exe", 4, "quarantine"),
        ("a.pdf", 0, "quarantine"),
        ("a.nwd", 4, "native_conversion"),
        ("a.nc1", 4, "fabrication_review"),
        ("a.txt", 4, "native_text"),
        ("a.pdf", 600 * 1024 * 1024, "document_split"),
        ("__MACOSX/._file.zip", 4, "quarantine"),
    ],
)
def test_every_format_has_explicit_disposition(name, size, route) -> None:
    assert disposition(name, size)[0] == route


def test_refuse_output_inside_corpus_or_existing_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        discover(tmp_path, tmp_path / "report", "test")
    root = tmp_path / "corpus"
    root.mkdir()
    with pytest.raises(ValueError, match="never overwritten"):
        discover(root, tmp_path, "test")
