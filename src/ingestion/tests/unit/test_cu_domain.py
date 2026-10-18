from __future__ import annotations

import pytest

from ingest_pipeline.domain.cu import (
    AnalyzerRegistry,
    flatten_fields,
    parse_source,
    span_text,
    subset_mismatches,
    usage_from,
)
from ingest_pipeline.domain.errors import ConfigurationError
from ingest_pipeline.domain.quality import UnitQuality, failed_rule, measure_layout
from ingest_pipeline.domain.routing import (
    CuProfileRule,
    EscalationRule,
    Family,
    PageSignals,
    PreflightReport,
    Route,
    RoutingPolicy,
    UnitKind,
    plan_id_for,
    plan_units,
)
from ingest_pipeline.domain.sniff import FileKind

HASH = "a" * 64
REGISTRY = {
    "apiVersion": "2025-11-01",
    "modelDeployments": {"gpt-5.4-mini": "dep-mini", "text-embedding-3-large": "dep-emb"},
    "modelVersions": {"dep-mini": "2026-03-17"},
    "prebuiltAnalyzers": ["prebuilt-documentSearch"],
    "analyzers": {
        "tizert_pfd_sheet_v1": {
            "baseAnalyzerId": "prebuilt-document",
            "models": {"completion": "gpt-5.4-mini", "embedding": "text-embedding-3-large"},
            "config": {"returnDetails": True},
            "fieldSchema": {
                "fields": {
                    "DrawingNumber": {"type": "string", "method": "extract"},
                    "Summary": {"type": "string"},
                }
            },
        }
    },
}


def test_sources_decode_polygons_boxes_and_several_regions() -> None:
    regions = parse_source("D(3,1,2,5,2,5,4,1,4);D(4,1,2,3,4)")
    assert regions == [
        (3, (1.0, 2.0, 5.0, 2.0, 5.0, 4.0, 1.0, 4.0)),
        (4, (1.0, 2.0, 4.0, 2.0, 4.0, 6.0, 1.0, 6.0)),
    ]
    assert parse_source(None) == []
    assert parse_source("D(2,1,2,3)") == [(2, ())]


def test_printed_text_comes_from_spans_in_code_points() -> None:
    markdown = "Révision: 00\nDébit 5.20 t/h"
    assert span_text(markdown, [{"offset": 19, "length": 4}]) == "5.20"
    assert span_text(markdown, [{"offset": 10, "length": 2}, {"offset": 13, "length": 5}]) == (
        "00 Débit"
    )
    assert span_text(markdown, [{"offset": 90, "length": 2}]) is None


def test_nested_fields_flatten_to_leaf_paths() -> None:
    fields = {
        "Tags": {"type": "array", "valueArray": [{"type": "string", "valueString": "A"}]},
        "Rows": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {"Value": {"type": "number", "valueNumber": 5.2}},
                }
            ],
        },
    }
    assert [(top, path) for top, path, _ in flatten_fields(fields)] == [
        ("Tags", "Tags[0]"),
        ("Rows", "Rows[0].Value"),
    ]


def test_usage_sums_meters_and_splits_tokens() -> None:
    usage = usage_from(
        {
            "documentPagesStandard": 2,
            "documentPagesMinimal": 1,
            "contextualizationTokens": 3000,
            "tokens": {"gpt-5.4-mini-input": 100, "gpt-5.4-mini-output": 20, "emb-input": 5},
        },
        requests=3,
    )
    assert (usage.pages, usage.input_tokens, usage.output_tokens, usage.requests) == (
        3, 105, 20, 3
    )
    assert usage.details["contextualization_tokens"] == 3000


def test_registry_resolves_models_methods_and_refuses_unknown_analyzers() -> None:
    registry = AnalyzerRegistry.load(REGISTRY)
    profile = registry.profile("tizert_pfd_sheet_v1")
    assert profile.deployments == {"gpt-5.4-mini": "dep-mini", "text-embedding-3-large": "dep-emb"}
    assert profile.model_versions == {"dep-mini": "2026-03-17"}
    assert profile.field_methods == {"DrawingNumber": "extract", "Summary": "generate"}
    with pytest.raises(ConfigurationError):
        registry.profile("prebuilt-documentSearch")
    registry.add_prebuilt("prebuilt-documentSearch", {"models": {}, "createdAt": "x"})
    assert registry.profile("prebuilt-documentSearch").prebuilt
    with pytest.raises(ConfigurationError):
        registry.add_prebuilt("prebuilt-invoice", {})


def test_definition_changes_change_the_profile_identity() -> None:
    changed = {
        **REGISTRY,
        "analyzers": {
            "tizert_pfd_sheet_v1": {
                **REGISTRY["analyzers"]["tizert_pfd_sheet_v1"],  # type: ignore[index]
                "description": "v1.1",
            }
        },
    }
    before = AnalyzerRegistry.load(REGISTRY).profile("tizert_pfd_sheet_v1").identity()
    after = AnalyzerRegistry.load(changed).profile("tizert_pfd_sheet_v1").identity()
    assert before != after


def test_subset_comparison_ignores_service_defaults() -> None:
    expected = {"config": {"returnDetails": True}, "fieldSchema": {"fields": {"A": {}}}}
    actual = {"config": {"returnDetails": True, "enableBarcode": True}, "fieldSchema": expected[
        "fieldSchema"
    ]}
    assert subset_mismatches(expected, actual) == []
    assert subset_mismatches({"config": {"returnDetails": False}}, actual) == [
        "$.config.returnDetails"
    ]


def _report(pages: list[PageSignals]) -> PreflightReport:
    return PreflightReport(
        content_hash=HASH,
        size_bytes=1000,
        snapshot_id="s",
        blob_name="b.pdf",
        extension=".pdf",
        file_kind=FileKind.PDF,
        family=Family.PDF,
        page_count=len(pages),
        pages=tuple(pages),
    )


def _page(number: int, *, drawing: bool = False, text: int = 0) -> PageSignals:
    width, height = (1191.0, 842.0) if drawing else (595.0, 842.0)
    return PageSignals(
        page=number, width_pt=width, height_pt=height, text_chars=text, image_objects=1
    )


def test_cu_rules_move_di_units_and_content_overrides_win() -> None:
    report = _report([_page(1, text=500), _page(2, drawing=True), _page(3)])
    policy = RoutingPolicy(
        version="cu-first",
        cu_profiles=(
            CuProfileRule(unit_kinds=frozenset({UnitKind.DRAWING_SHEET}), analyzer_id="draw"),
        ),
    )
    units = plan_units(report, policy)
    assert [(unit.route, unit.locator.get("profile")) for unit in units] == [
        (Route.NATIVE_PDF_TEXT, None),
        (Route.CU_ANALYZE, "draw"),
        (Route.DI_LAYOUT, None),
    ]
    assert units[1].locator["replaces"] == Route.DI_LAYOUT_DRAWING.value
    override = policy.model_copy(update={"cu_content_profiles": {HASH: "pfd"}})
    assert [unit.locator.get("profile") for unit in plan_units(report, override)] == [
        None, "pfd", "pfd"
    ]
    assert plan_id_for(report, policy) != plan_id_for(report, override)


def test_escalation_rules_keep_units_on_di_with_their_gate() -> None:
    report = _report([_page(1, text=500), _page(2, drawing=True)])
    rule = EscalationRule(
        unit_kinds=frozenset({UnitKind.DRAWING_SHEET}), analyzer_id="draw", min_words_per_page=40
    )
    units = plan_units(report, RoutingPolicy(version="selective", escalation=(rule,)))
    assert [unit.route for unit in units] == [Route.NATIVE_PDF_TEXT, Route.DI_LAYOUT_DRAWING]
    assert "escalate" not in units[0].locator
    assert EscalationRule.model_validate(units[1].locator["escalate"]) == rule
    both = RoutingPolicy(
        version="both",
        cu_profiles=(
            CuProfileRule(unit_kinds=frozenset({UnitKind.DRAWING_SHEET}), analyzer_id="cu"),
        ),
        escalation=(rule,),
    )
    [_, drawing] = plan_units(report, both)
    assert drawing.route is Route.CU_ANALYZE
    assert "escalate" not in drawing.locator


def test_quality_gate_escalates_sparse_low_confidence_or_unidentified_units() -> None:
    analyze = {
        "content": "SP0904-6210-PFD-003",
        "pages": [{"words": [{"confidence": 0.9}, {"confidence": 0.5}]}],
    }
    quality = measure_layout(analyze)
    assert (quality.words, quality.mean_word_confidence) == (2, pytest.approx(0.7))
    rule = EscalationRule(
        unit_kinds=frozenset({UnitKind.DRAWING_SHEET}), analyzer_id="draw", min_words_per_page=3
    )
    assert failed_rule(quality, "drawing_sheet", [rule]) == rule
    assert failed_rule(quality, "pdf_pages", [rule]) is None
    confident = UnitQuality(pages=1, words=50, mean_word_confidence=0.99, text="no id")
    identified = rule.model_copy(
        update={"min_words_per_page": 0, "identifier_pattern": r"SP0904-\d{4}"}
    )
    assert failed_rule(confident, "drawing_sheet", [identified]) == identified
    assert failed_rule(quality, "drawing_sheet", [identified]) is None
