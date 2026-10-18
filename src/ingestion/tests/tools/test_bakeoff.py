"""Phase 3 bake-off tooling: sample, gold validation, scoring and rendering (offline)."""

from __future__ import annotations

import json

import pytest
from fixture_builders import repo_file

from tizertmaint_pipeline.config import load_policies
from tizertmaint_pipeline.domain.evidence import (
    BlockKind,
    EvidenceBlock,
    EvidenceKind,
    EvidencePage,
    EvidenceUnit,
    FieldObservation,
)
from tizertmaint_pipeline.tools.bakeoff.__main__ import (
    REPORT_BEGIN,
    REPORT_END,
    extract_report,
    load_gold,
)
from tizertmaint_pipeline.tools.bakeoff.model import DocumentEvidence, GoldItem, Report
from tizertmaint_pipeline.tools.bakeoff.report import render_markdown
from tizertmaint_pipeline.tools.bakeoff.sample import PILOT_PREFIX, build_sample
from tizertmaint_pipeline.tools.bakeoff.score import contains, page_evidence, score_arm

HASH = "c" * 64
CAPS = {"cu_pages": 600, "tokens": 6_000_000}


def _sample():
    review = json.loads(
        repo_file("Docs/knowledge-graph/09-source-review-evidence.json").read_text("utf-8")
    )
    policies = load_policies(repo_file("infra/content-understanding/bakeoff-policies.json"))
    return build_sample(review, policies, caps=CAPS)


def test_sample_covers_the_review_within_the_spend_cap() -> None:
    sample = _sample()
    ids = [document.id for document in sample.documents]
    assert ids[0] == "PFD"
    assert [exclusion.id for exclusion in sample.excluded] == ["C08"]
    pfd = sample.document("PFD")
    assert pfd.blob_name.startswith(PILOT_PREFIX)
    assert pfd.upload_from == "Docs/Sch\u00e9mas PFD Tizert Usine 3.pdf"
    assert (pfd.split_of(4), pfd.split_of(22)) == ("tuning", "held_out")
    assert sample.document("C02").split == "tuning"
    assert len(sample.cu_arms) == 3
    assert "bakeoff-cu-first-v1" not in sample.arms
    assert "bakeoff-selective-v1" not in sample.arms
    assert "routing-v1" not in sample.cu_arms
    assert sample.estimate["cu_pages_upper_bound"] <= CAPS["cu_pages"]
    policies = load_policies(repo_file("infra/content-understanding/bakeoff-policies.json"))
    with pytest.raises(ValueError, match="exceeds the cap"):
        build_sample(
            json.loads(
                repo_file("Docs/knowledge-graph/09-source-review-evidence.json").read_text("utf-8")
            ),
            policies,
            caps={"cu_pages": 100, "tokens": 1},
        )


def test_reviewed_gold_set_matches_the_sample() -> None:
    sample = _sample()
    gold = load_gold(repo_file("data/Golden/extraction/gold-v1.jsonl").read_text("utf-8"), sample)
    assert len(gold) >= 100
    assert {item.status for item in gold} == {"sme_checked", "rejected"}
    assert all(item.note for item in gold if item.status == "rejected")
    adversarial = {item.id for item in gold if item.check == "absent"}
    assert {"pfd-p04-absent-6032-ZM-2427", "pfd-p25-absent-6610-TK-5410"} <= adversarial
    splits = {sample.document(item.doc).split_of(item.page) for item in gold}
    assert splits == {"tuning", "held_out"}


def test_values_match_only_on_their_own() -> None:
    assert contains("Tag 6140-ZM-2427 hydraulic", "6140-ZM-2427")
    assert contains("6140\u2013ZM\u20132427", "6140-ZM-2427")
    assert not contains("6140-ZM-24270", "6140-ZM-2427")
    assert contains("S 261 t/h", "261")
    assert not contains("2610", "261")
    assert not contains("261,5", "261")
    assert contains("debit 90,0 t/h", "90,0")
    assert not contains("90,05", "90,0")


def _unit(method: str, blocks: tuple[EvidenceBlock, ...], fields=()) -> EvidenceUnit:
    return EvidenceUnit(
        unit_id=f"u-{method}",
        plan_id="p",
        content_hash=HASH,
        route="r",
        extraction_method=method,
        normalizer="test",
        pages=(EvidencePage(page=4),),
        blocks=blocks,
        fields=fields,
    )


def _items() -> list[GoldItem]:
    def item(item_id: str, check: str, value: str, **extra) -> GoldItem:
        return GoldItem(
            id=item_id, doc="PFD", page=4, check=check, value=value, category="tag",
            seed="test", **extra,
        )

    return [
        item("literal", "literal", "6140-ZM-2427"),
        item("generated-only", "literal", "6140-CR-2426"),
        item("absent", "absent", "6032-ZM-2427"),
        item("field", "field", "6140-ZM-2427", field="EquipmentTags"),
    ]


def test_scoring_separates_source_generated_and_field_evidence() -> None:
    sample = _sample()
    cu = page_evidence(
        [
            _unit(
                "cu_analyze",
                (
                    EvidenceBlock(evidence_id="a", kind=BlockKind.PARAGRAPH,
                                  text="Pack 6140-ZM-2427", pages=(4,)),
                    EvidenceBlock(evidence_id="b", kind=BlockKind.PARAGRAPH,
                                  text="Crusher 6140-CR-2426 next to 6032-ZM-2427", pages=(4,),
                                  evidence_kind=EvidenceKind.GENERATED_DESCRIPTION),
                ),
                fields=(
                    FieldObservation(
                        path="EquipmentTags[0]", field="EquipmentTags", method="extract",
                        evidence_kind=EvidenceKind.EXTRACTED_FIELD, value_type="string",
                        value="6140-ZM-2427", raw_text="6140-ZM-2427", pages=(4,),
                        analyzer_id="tizert_pfd_sheet_v1",
                    ),
                ),
            )
        ]
    )
    evidence = {"PFD": DocumentEvidence(doc="PFD", found=True, complete=True, pages=cu)}
    score = score_arm("bakeoff-cu-first-v1r", _items(), evidence, sample)
    outcomes = {item.id: (item.outcome, item.where) for item in score.items}
    assert outcomes["literal"] == ("pass", ("source", "field"))
    assert outcomes["generated-only"] == ("fail", ("generated",))
    assert outcomes["absent"] == ("fail", ("generated",))
    assert outcomes["field"][0] == "pass"
    assert score.metrics["tuning"]["literal_only_generated"] == 1

    di = page_evidence(
        [_unit("di_layout", (EvidenceBlock(evidence_id="a", kind=BlockKind.PARAGRAPH,
                                           text="6140-ZM-2427", pages=(4,)),))]
    )
    baseline = score_arm(
        "routing-v1", _items(),
        {"PFD": DocumentEvidence(doc="PFD", found=True, pages=di)}, sample,
    )
    assert {item.id: item.outcome for item in baseline.items} == {
        "literal": "pass",
        "generated-only": "fail",
        "absent": "pass",
        "field": "not_applicable",
    }
    drawings = score_arm(
        "bakeoff-drawings-cu-v2", _items(),
        {"PFD": DocumentEvidence(doc="PFD", found=True, pages=di)}, sample,
    )
    assert {item.id: item.outcome for item in drawings.items}["field"] == "not_applicable"
    missing = score_arm("bakeoff-drawings-cu-v2", _items(), {}, sample)
    assert {item.outcome for item in missing.items} == {"missing_evidence"}

    report = Report(
        generated_at="2026-10-10T00:00:00+00:00", sample_sha256="a" * 64, gold_sha256="b" * 64,
        gold_items=4, arms=(score, baseline),
    )
    markdown = render_markdown(report)
    assert "| bakeoff-cu-first-v1r | tuning | 50.0% |" in markdown
    assert "generated-only" in markdown
    logs = (
        '{"level": "INFO", "message": "CU analyzers verified"}\n'
        f"{REPORT_BEGIN}\n{report.model_dump_json(by_alias=True, indent=1)}\n{REPORT_END}\n"
        '{"level": "INFO", "message": "shutdown"}\n'
    )
    assert extract_report(logs) == report
