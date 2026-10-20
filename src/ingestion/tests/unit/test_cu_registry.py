from __future__ import annotations

import copy
import json

import pytest
from evidence_fakes import FakeContentAnalysis
from fixture_builders import repo_file

from ingest_pipeline.adapters.azure_ai.cu_registry import load_registry, verify_registry
from ingest_pipeline.domain.cu import AnalyzerRegistry
from ingest_pipeline.domain.errors import ConfigurationError

REGISTRY = "infra/content-understanding/ingestion-pilot.json"
PREBUILT = {
    "analyzerId": "prebuilt-documentSearch",
    "status": "ready",
    "createdAt": "2025-11-01T00:00:00Z",
    "models": {"completion": "gpt-4.1-mini", "embedding": "text-embedding-3-large"},
    "config": {"enableFigureDescription": True},
}


def _live(document: dict) -> FakeContentAnalysis:
    analyzers = {
        name: {**copy.deepcopy(spec), "analyzerId": name, "status": "ready", "tags": {}}
        for name, spec in document["analyzers"].items()
    }
    analyzers["prebuilt-documentSearch"] = PREBUILT
    return FakeContentAnalysis(
        analyzers=analyzers,
        default_settings={"modelDeployments": dict(document["modelDeployments"])},
    )


async def test_reviewed_registry_loads_and_verifies() -> None:
    path = repo_file(REGISTRY)
    registry = await load_registry(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    check = await verify_registry(_live(document), registry)
    assert check.problems == {}
    assert check.verified == sorted([*document["analyzers"], "prebuilt-documentSearch"])
    profile = check.catalog.profile("tizert_pfd_sheet_v1")
    assert profile.model_versions == {
        "crv-gpt-5.4-mini": "2026-03-17",
        "crv-embedding-3-large": "1",
    }
    assert profile.field_methods["EquipmentTags"] == "extract"
    prebuilt = check.catalog.profile("prebuilt-documentSearch")
    assert prebuilt.prebuilt
    assert prebuilt.deployments == {
        "completion": "crv-gpt-5.4-mini",
        "embedding": "crv-embedding-3-large",
    }


async def test_drifted_missing_or_unready_analyzers_are_left_out() -> None:
    document = json.loads(repo_file(REGISTRY).read_text(encoding="utf-8"))
    live = _live(document)
    live.analyzers["tizert_pfd_sheet_v1"]["description"] = "edited in the portal"
    live.analyzers["tizert_classify_v1"]["status"] = "creating"
    del live.analyzers["tizert_drawing_search_v1"]
    check = await verify_registry(live, AnalyzerRegistry.load(document))
    assert check.verified == [
        "prebuilt-documentSearch",
        "tizert_doc_fields_v1",
        "tizert_pfd_sheet_v2",
        "tizert_pfd_sheet_v3",
    ]
    assert check.problems["tizert_pfd_sheet_v1"] == ["$.description"]
    assert check.problems["tizert_classify_v1"] == ["$.status=creating"]
    assert "404" in check.problems["tizert_drawing_search_v1"][0]
    with pytest.raises(ConfigurationError):
        check.catalog.profile("tizert_pfd_sheet_v1")


async def test_model_default_drift_disables_every_analyzer() -> None:
    document = json.loads(repo_file(REGISTRY).read_text(encoding="utf-8"))
    live = _live(document)
    live.default_settings["modelDeployments"]["gpt-5.4-mini"] = "someone-elses-deployment"
    check = await verify_registry(live, AnalyzerRegistry.load(document))
    assert check.verified == []
    assert check.problems == {"defaults": ["$.modelDeployments.gpt-5.4-mini"]}


class _NoDefaults(FakeContentAnalysis):
    async def defaults(self) -> dict:
        from ingest_pipeline.domain.errors import InvalidInputError

        raise InvalidInputError("CU defaults read failed with HTTP 400: DefaultsNotSet")


async def test_extraction_only_registry_needs_no_model_defaults() -> None:
    registry = await load_registry(repo_file("infra/content-understanding/crv-cu-registry.json"))
    live = _NoDefaults(analyzers={"prebuilt-layout": {
        "analyzerId": "prebuilt-layout", "status": "ready", "config": {"enableLayout": True},
    }})
    check = await verify_registry(live, registry)
    assert check.problems == {}
    profile = check.catalog.profile("prebuilt-layout")
    assert profile.prebuilt
    assert profile.completion_model is None
    with pytest.raises(Exception, match="DefaultsNotSet"):
        await verify_registry(_NoDefaults(), await load_registry(repo_file(REGISTRY)))
