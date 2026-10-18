from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ingest_pipeline.adapters.postgres.migrations import (
    BASELINE_VERSION,
    Migration,
    apply_migrations,
    load_migrations,
)
from ingest_pipeline.adapters.telemetry.json_logging import JsonFormatter
from ingest_pipeline.cli import build_parser
from ingest_pipeline.config import PipelineSettings, require
from ingest_pipeline.domain.errors import ConfigurationError, IntegrityError
from ingest_pipeline.domain.states import WorkerPool


def test_packaged_migrations_load_in_order_with_lf_checksums() -> None:
    migrations = load_migrations()
    assert migrations[0].version == "0003_ingest_ledger"
    assert all(migration.version > BASELINE_VERSION for migration in migrations)
    assert "\r\n" not in migrations[0].sql
    assert Migration("v", "a\nb").sha256 == Migration("v", "a\r\nb".replace("\r\n", "\n")).sha256


def test_load_migrations_rejects_baseline_versions(tmp_path: Path) -> None:
    (tmp_path / "0002_shadow.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_migrations(tmp_path)


class _Connection:
    def __init__(self, registry: dict[str, str] | None, *, has_table: bool = True) -> None:
        self.registry = registry
        self.has_table = has_table
        self.executed: list[str] = []
        self.committed = False

    @asynccontextmanager
    async def transaction(self):
        snapshot = (dict(self.registry or {}), list(self.executed))
        try:
            yield
        except BaseException:
            self.registry = snapshot[0] if self.registry is not None else None
            self.executed = snapshot[1]
            raise
        self.committed = True

    async def execute(self, query: str, *args: Any) -> str:
        if query.startswith("INSERT INTO ingest.schema_migration"):
            assert self.registry is not None
            self.registry[args[0]] = args[1]
        elif not query.startswith("SELECT pg_advisory"):
            self.executed.append(query)
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        return "ingest.schema_migration" if self.has_table else None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, str]]:
        return [{"version": v, "sha256": s} for v, s in (self.registry or {}).items()]

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return None


MIGRATIONS = [Migration("0003_a", "CREATE TABLE a();"), Migration("0004_b", "CREATE TABLE b();")]


async def test_apply_migrations_is_idempotent_and_reports_progress() -> None:
    connection = _Connection({BASELINE_VERSION: "x" * 64, "0003_a": MIGRATIONS[0].sha256})

    report = await apply_migrations(connection, MIGRATIONS, dry_run=False)

    assert report.applied == ["0004_b"]
    assert report.already_applied == ["0003_a"]
    assert connection.executed == ["CREATE TABLE b();"]
    assert connection.committed


async def test_apply_migrations_refuses_checksum_drift_and_missing_baseline() -> None:
    drifted = _Connection({BASELINE_VERSION: "x" * 64, "0003_a": "0" * 64})
    with pytest.raises(IntegrityError, match="changed after it was applied"):
        await apply_migrations(drifted, MIGRATIONS, dry_run=False)
    assert drifted.executed == []

    with pytest.raises(ConfigurationError, match="Baseline"):
        await apply_migrations(_Connection({}), MIGRATIONS, dry_run=False)
    with pytest.raises(ConfigurationError, match="before pipeline"):
        await apply_migrations(_Connection(None, has_table=False), MIGRATIONS, dry_run=False)


async def test_dry_run_rolls_back_and_reports_unknown_versions() -> None:
    connection = _Connection({BASELINE_VERSION: "x" * 64, "0099_future": "f" * 64})

    report = await apply_migrations(connection, MIGRATIONS, dry_run=True)

    assert report.applied == ["0003_a", "0004_b"]
    assert report.unknown_applied == ["0099_future"]
    assert connection.executed == []
    assert "0003_a" not in (connection.registry or {})


def test_settings_read_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIZ_DB__HOST", "psql.example")
    monkeypatch.setenv("TIZ_BUS__QUEUES", json.dumps({"io": "custom-io"}))
    monkeypatch.setenv("TIZ_RUNTIME__LEASE_SECONDS", "90")

    settings = PipelineSettings()

    assert settings.db.host == "psql.example"
    assert settings.bus.queue_for(WorkerPool.IO) == "custom-io"
    assert settings.runtime.lease_seconds == 90
    with pytest.raises(ConfigurationError, match="QUEUES"):
        settings.bus.queue_for(WorkerPool.CPU)


def test_settings_validate_heartbeat_and_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIZ_RUNTIME__LEASE_SECONDS", "40")
    monkeypatch.setenv("TIZ_RUNTIME__HEARTBEAT_SECONDS", "30")
    with pytest.raises(ValidationError, match="heartbeat"):
        PipelineSettings()
    with pytest.raises(ConfigurationError, match="TIZ_DB__HOST"):
        require(None, "TIZ_DB__HOST")


def test_json_formatter_adds_context_and_redacts_content() -> None:
    record = logging.LogRecord(
        "ingest_pipeline", logging.INFO, __file__, 1, "Stage %s", ("done",), None
    )
    record.job_id = "job-1"
    record.content = "confidential extracted text"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "Stage done"
    assert payload["job_id"] == "job-1"
    assert payload["content"] == "[redacted]"
    assert payload["level"] == "INFO"


def test_cli_parser_exposes_pipeline_commands() -> None:
    parser = build_parser()
    worker = parser.parse_args(["worker", "--pool", "io", "--exit-when-idle"])
    assert (worker.pool, worker.exit_when_idle) == ("io", True)
    enqueue = parser.parse_args(
        ["enqueue-snapshots", "--prefix", "2-Zone Broyage/", "--limit", "5"]
    )
    assert (enqueue.prefix, enqueue.limit) == ("2-Zone Broyage/", 5)
    with pytest.raises(SystemExit):
        parser.parse_args(["worker", "--pool", "gpu"])


def test_cli_parser_exposes_operator_commands() -> None:
    parser = build_parser()
    replay = parser.parse_args(
        ["replay", "--job-id", "job-1", "--reason", "portal shows no operation", "--actor", "ops"]
    )
    assert (replay.job_id, replay.actor) == ("job-1", "ops")
    with pytest.raises(SystemExit):
        parser.parse_args(["replay", "--job-id", "job-1", "--reason", "x"])
    legacy = parser.parse_args(["reconcile-legacy"])
    assert legacy.apply is False
    status = parser.parse_args(["status", "--failures", "5"])
    assert status.failures == 5
    enqueue = parser.parse_args(["enqueue-snapshots", "--blob-list", "blobs.txt"])
    assert enqueue.blob_list == "blobs.txt"


def test_blob_list_keeps_utf8_names_and_drops_blanks(tmp_path: Path) -> None:
    from ingest_pipeline.cli import read_blob_list

    names = tmp_path / "blobs.txt"
    raw = "\ufeff2-Zone Broyage/7-Schéma élec & instrum/a.pdf\r\n\n# comment\nb.xlsx\nb.xlsx\n"
    names.write_bytes(raw.encode())
    assert read_blob_list(str(names)) == [
        "2-Zone Broyage/7-Schéma élec & instrum/a.pdf",
        "b.xlsx",
    ]
    names.write_text("\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_blob_list(str(names))


def test_evidence_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIZ_DI__PAGE_BUDGET", "500")
    monkeypatch.setenv("TIZ_DI__DRAWING_FEATURES", '["ocrHighResolution"]')
    monkeypatch.setenv("TIZ_ROUTING__ENABLED_ROUTES", '["native_pdf_text", "di_layout"]')
    monkeypatch.setenv("TIZ_CHUNKING__ALLOW_PARTIAL_PROJECTION", "true")

    settings = PipelineSettings()

    assert settings.di.page_budget == 500
    assert settings.di.drawing_features == ("ocrHighResolution",)
    assert {route.value for route in settings.routing.enabled_routes} == {
        "native_pdf_text",
        "di_layout",
    }
    assert settings.chunking.allow_partial_projection is True


def test_evidence_settings_reject_unsafe_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIZ_DI__POLL_SCHEDULE_SECONDS", "[1, 5]")
    with pytest.raises(ValidationError, match="2 seconds"):
        PipelineSettings()
    monkeypatch.delenv("TIZ_DI__POLL_SCHEDULE_SECONDS")
    monkeypatch.setenv("TIZ_CHUNKING__OVERLAP_TOKENS", "700")
    with pytest.raises(ValidationError, match="overlap"):
        PipelineSettings()


def test_registry_registers_every_evidence_stage() -> None:
    from ingest_pipeline.domain.states import StageName
    from ingest_pipeline.runtime.bootstrap import build_registry

    registry = build_registry(PipelineSettings())
    for stage in (
        StageName.SNAPSHOT,
        StageName.PREFLIGHT,
        StageName.PLAN,
        StageName.NATIVE_PARSE,
        StageName.DI_SUBMIT,
        StageName.DI_POLL,
        StageName.CU_SUBMIT,
        StageName.CU_POLL,
        StageName.FIGURE_ASSETS,
        StageName.NORMALIZE,
        StageName.CHUNK,
        StageName.EMBED,
        StageName.PROJECT_SEARCH,
    ):
        assert registry.get(stage).handler.version


def test_cu_settings_and_arm_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    from fixture_builders import repo_file

    from ingest_pipeline.config import load_policies
    from ingest_pipeline.domain.routing import Route, UnitKind

    policies_path = repo_file("infra/content-understanding/bakeoff-policies.json")
    monkeypatch.setenv("TIZ_CU__ENDPOINT", "https://cu.example")
    monkeypatch.setenv("TIZ_CU__REGISTRY_PATH", "/etc/tizert/cu/ingestion-pilot.json")
    monkeypatch.setenv("TIZ_CU__PAGE_BUDGET", "600")
    monkeypatch.setenv("TIZ_CU__TOKEN_BUDGET", "6000000")
    monkeypatch.setenv("TIZ_POLICIES_PATH", str(policies_path))
    settings = PipelineSettings()
    assert (settings.cu.page_budget, settings.cu.token_budget) == (600, 6_000_000)
    assert settings.cu.processing_location == "dataZone"

    policies = load_policies(settings.policies_path)
    assert set(policies.release_ids()) == {
        "routing-v1",
        "bakeoff-cu-first-v1",
        "bakeoff-cu-first-v1r",
        "bakeoff-selective-v1",
        "bakeoff-docsearch-v1",
        "bakeoff-drawings-cu-v2",
        "bakeoff-drawings-cu-v3",
    }
    assert set(policies.active()) == {
        "routing-v1",
        "bakeoff-cu-first-v1r",
        "bakeoff-drawings-cu-v2",
        "bakeoff-drawings-cu-v3",
    }
    assert settings.routing.version == "crv-routing-v1"
    assert settings.routing.preserve_layout
    cu_first = policies.get("bakeoff-cu-first-v1").policy
    assert cu_first.cu_profiles[0].unit_kinds == frozenset({UnitKind.DRAWING_SHEET})
    assert Route.CU_ANALYZE in cu_first.enabled_routes
    with pytest.raises(ConfigurationError):
        policies.get("bakeoff-unknown")

    monkeypatch.delenv("TIZ_CU__REGISTRY_PATH")
    with pytest.raises(ValidationError, match="registry_path"):
        PipelineSettings()


def test_default_routing_policy_is_selected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from fixture_builders import repo_file

    from ingest_pipeline.config import load_policies

    path = repo_file("infra/content-understanding/routing-policies.json")
    monkeypatch.setenv("TIZ_POLICIES_PATH", str(path))
    monkeypatch.setenv("TIZ_ROUTING_POLICY", "routing-v2")
    settings = PipelineSettings()
    policy = settings.default_routing()
    assert policy.version == "routing-v2"
    assert [rule.analyzer_id for rule in policy.cu_profiles] == ["tizert_pfd_sheet_v2"]
    assert load_policies(path).release_ids()["routing-v2"] == "corpus-v2-20261009"

    monkeypatch.setenv("TIZ_ROUTING_POLICY", "")
    assert PipelineSettings().default_routing() == PipelineSettings().routing

    monkeypatch.setenv("TIZ_ROUTING_POLICY", "routing-v9")
    with pytest.raises(ConfigurationError):
        PipelineSettings().default_routing()

    monkeypatch.delenv("TIZ_POLICIES_PATH")
    monkeypatch.setenv("TIZ_ROUTING_POLICY", "routing-v2")
    with pytest.raises(ValidationError, match="policies_path"):
        PipelineSettings()


def test_policy_registry_rejects_mismatched_versions_and_shared_releases() -> None:
    from ingest_pipeline.config import PolicyRegistry

    with pytest.raises(ValidationError, match="declares version"):
        PolicyRegistry.model_validate(
            {"policies": {"a": {"release_id": "a", "policy": {"version": "b"}}}}
        )
    with pytest.raises(ValidationError, match="own release_id"):
        PolicyRegistry.model_validate(
            {
                "policies": {
                    "a": {"release_id": "same", "policy": {"version": "a"}},
                    "b": {"release_id": "same", "policy": {"version": "b"}},
                }
            }
        )


def test_cli_parser_exposes_enqueue_arm() -> None:
    parser = build_parser()
    arm = parser.parse_args(
        ["enqueue-arm", "--policy", "bakeoff-cu-first-v1", "--blob-list", "sample.txt"]
    )
    assert (arm.policy, arm.blob_list) == ("bakeoff-cu-first-v1", "sample.txt")
    with pytest.raises(SystemExit):
        parser.parse_args(["enqueue-arm", "--blob-list", "sample.txt"])
    assert parser.parse_args(["cu-check"]).command == "cu-check"


def test_legacy_classification() -> None:
    from ingest_pipeline.domain.legacy import LegacyItem, LegacyState

    def item(chunks: int, evidence: str | None) -> LegacyItem:
        return LegacyItem("a" * 64, "indexed", chunks, 3, 0, evidence)

    assert item(0, None).state is LegacyState.UNKNOWN
    assert item(4, None).state is LegacyState.EVIDENCE_AVAILABLE
    assert item(4, "evd:1").state is LegacyState.VERIFIED
    assert item(0, None).legacy_key == "v1:" + "a" * 64
