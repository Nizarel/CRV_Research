"""Typed pipeline settings from ``TIZ_*`` environment variables (nested with ``__``).

Credentials are never settings: Azure services use Entra workload identity. The optional
``db.dsn`` exists only for local and containerized integration tests, and an external billing
account names the mounted file holding its API key, never the key itself.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.errors import ConfigurationError
from .domain.retry import RetryPolicy
from .domain.routing import RoutingPolicy
from .domain.states import WorkerPool

MIB = 1024 * 1024
T = TypeVar("T")


def require(value: T | None, name: str) -> T:
    if value is None or value == "":
        raise ConfigurationError(f"Missing required setting {name}")
    return value


class DatabaseSettings(BaseModel):
    host: str | None = None
    port: int = 5432
    database: str = "ingestion-catalog"
    user: str | None = None
    dsn: SecretStr | None = None
    min_pool_size: int = Field(default=1, ge=0)
    max_pool_size: int = Field(default=10, ge=1)
    command_timeout_seconds: float = Field(default=60.0, gt=0)
    statement_timeout_ms: int = Field(default=30_000, ge=0)
    application_name: str = "tizert-pipeline"


class BusSettings(BaseModel):
    namespace: str | None = None
    queues: dict[WorkerPool, str] = Field(
        default_factory=lambda: {pool: f"ingest-{pool.value}" for pool in WorkerPool}
    )
    max_wait_seconds: float = Field(default=30.0, gt=0)
    max_lock_renewal_seconds: float = Field(default=600.0, gt=0)

    def queue_for(self, pool: WorkerPool) -> str:
        return require(self.queues.get(pool), f"TIZ_BUS__QUEUES[{pool.value}]")


class BlobSettings(BaseModel):
    account_url: str | None = None
    source_container: str | None = None
    artifact_container: str = "ingest-artifacts"
    max_source_bytes: int = Field(default=1024 * MIB, gt=0)
    max_artifact_bytes: int = Field(default=512 * MIB, gt=0)
    """Raw DI results and figure sets are stored as artifacts; they are streamed, not buffered."""


class RuntimeSettings(BaseModel):
    worker_id: str = Field(default_factory=socket.gethostname)
    lease_seconds: float = Field(default=120.0, gt=0)
    heartbeat_seconds: float = Field(default=30.0, gt=0)
    stage_timeout_seconds: float = Field(default=900.0, gt=0)
    concurrency: int = Field(default=1, ge=1)
    dispatch_batch_size: int = Field(default=100, ge=1)
    dispatch_interval_seconds: float = Field(default=1.0, gt=0)
    dispatch_claim_seconds: float = Field(default=60.0, gt=0)
    reconcile_interval_seconds: float = Field(default=15.0, gt=0)
    reconcile_batch_size: int = Field(default=100, ge=1)
    stale_dispatch_seconds: float = Field(default=900.0, gt=0)
    retry_base_seconds: float = Field(default=2.0, gt=0)
    retry_max_seconds: float = Field(default=300.0, gt=0)
    retry_pause_seconds: float = Field(default=900.0, gt=0)
    max_failures: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _heartbeat_inside_lease(self) -> Self:
        if self.heartbeat_seconds * 2 > self.lease_seconds:
            raise ValueError("heartbeat_seconds must be at most half of lease_seconds")
        return self

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            base_delay_seconds=self.retry_base_seconds,
            max_delay_seconds=self.retry_max_seconds,
            pause_delay_seconds=self.retry_pause_seconds,
        )


class EmbeddingSettings(BaseModel):
    endpoint: str | None = None
    deployment: str | None = None
    model: str = "text-embedding-3-large"
    dimensions: int = Field(default=3072, ge=1)
    api_version: str = "2024-10-21"
    max_inputs_per_request: int = Field(default=16, ge=1, le=2048)
    max_tokens_per_request: int = Field(default=100_000, ge=1, le=300_000)
    max_tokens_per_input: int = Field(default=8191, ge=1, le=8192)


class SearchSettings(BaseModel):
    endpoint: str | None = None
    index_name: str = "crv-evidence-v1"
    corpus: Literal["crv", "maintenance"] = "crv"
    max_batch_documents: int = Field(default=100, ge=1, le=1000)
    max_batch_bytes: int = Field(default=4 * MIB, ge=1, le=16 * MIB)
    release_id: str = "crv-pilot-v1"
    access_scope_id: str = "unassigned"
    acl_policy_version: str = "phase2-deny-all"
    verify_deadline_seconds: float = Field(default=900.0, gt=0)
    retry_seconds: float = Field(default=20.0, gt=0)


class DocumentIntelligenceSettings(BaseModel):
    endpoint: str | None = None
    api_version: str = "2024-11-30"
    model_id: str = "prebuilt-layout"
    figures: bool = True
    features: tuple[str, ...] = ()
    drawing_features: tuple[str, ...] = ()
    """E.g. ``ocrHighResolution`` for small drawing text (billed as an add-on)."""
    locale: str | None = None
    max_inflight: int = Field(default=8, ge=1)
    page_budget: int | None = Field(default=None, ge=0)
    """Hard cap on billable DI pages for this deployment; unset means no cap."""
    poll_schedule_seconds: tuple[float, ...] = (2.0, 5.0, 13.0, 34.0, 60.0)
    poll_deadline_seconds: float = Field(default=3600.0, gt=0)
    result_retention_seconds: float = Field(default=23 * 3600.0, gt=0)
    max_result_bytes: int = Field(default=512 * MIB, gt=0)
    max_figure_bytes: int = Field(default=64 * MIB, gt=0)

    @model_validator(mode="after")
    def _polls_respect_service_limits(self) -> Self:
        if not self.poll_schedule_seconds or min(self.poll_schedule_seconds) < 2:
            raise ValueError("DI polls must be at least 2 seconds apart")
        return self


class ContentUnderstandingSettings(BaseModel):
    endpoint: str | None = None
    api_version: str = "2025-11-01"
    registry_path: Path | None = None
    """Reviewed analyzer registry (``infra/content-understanding``), mounted read-only."""
    processing_location: Literal["geography", "dataZone", "global"] = "dataZone"
    max_inflight: int = Field(default=4, ge=1)
    page_budget: int | None = Field(default=None, ge=0)
    """Hard cap on CU content-extraction pages for this deployment; unset means no cap."""
    token_budget: int | None = Field(default=None, ge=0)
    """Hard cap on completion-model input plus output tokens; unset means no cap."""
    est_tokens_per_page: int = Field(default=12_000, ge=0)
    poll_schedule_seconds: tuple[float, ...] = (2.0, 5.0, 13.0, 34.0, 60.0)
    poll_deadline_seconds: float = Field(default=3600.0, gt=0)
    max_result_bytes: int = Field(default=512 * MIB, gt=0)
    max_file_bytes: int = Field(default=64 * MIB, gt=0)
    capture_figures: bool = False
    figure_path_template: str = "figure-{id}"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.endpoint and self.registry_path is None:
            raise ValueError("A CU endpoint needs the reviewed analyzer registry_path")
        if not self.poll_schedule_seconds or min(self.poll_schedule_seconds) < 2:
            raise ValueError("CU polls must be at least 2 seconds apart")
        if "{id}" not in self.figure_path_template:
            raise ValueError("figure_path_template needs an {id} placeholder")
        return self


class PolicyEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    """Search release of this policy's rows, so arms never overwrite each other."""
    policy: RoutingPolicy
    superseded_by: str | None = None
    """A replaced arm keeps its release for in-flight work but is no longer sampled or scored."""


class PolicyRegistry(BaseModel):
    """Named routing policies (bake-off arms), each projected under its own release."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policies: dict[str, PolicyEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name, entry in self.policies.items():
            if entry.policy.version != name:
                raise ValueError(f"Policy {name} declares version {entry.policy.version}")
        releases = [entry.release_id for entry in self.policies.values()]
        if len(set(releases)) != len(releases):
            raise ValueError("Each policy needs its own release_id")
        for name, entry in self.policies.items():
            if entry.superseded_by is not None and entry.superseded_by not in self.policies:
                raise ValueError(f"Policy {name} is superseded by an unknown policy")
        return self

    def active(self) -> dict[str, PolicyEntry]:
        return {
            name: entry for name, entry in self.policies.items() if entry.superseded_by is None
        }

    def get(self, name: str) -> PolicyEntry:
        entry = self.policies.get(name)
        if entry is None:
            raise ConfigurationError(f"Routing policy {name} is not in the policy registry")
        return entry

    def release_ids(self) -> dict[str, str]:
        return {name: entry.release_id for name, entry in self.policies.items()}


def load_policies(path: Path | None) -> PolicyRegistry:
    if path is None:
        return PolicyRegistry()
    return PolicyRegistry.model_validate_json(path.read_text(encoding="utf-8"))


class ExternalAccount(BaseModel):
    """Document Intelligence and Content Understanding in another subscription (API key)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    budget_usd: float | None = Field(default=None, ge=0)
    di_endpoint: str | None = None
    cu_endpoint: str | None = None
    key_file: Path
    """Mounted Key Vault secret; the key is only ever sent to this account's endpoints."""
    cu_max_inflight: int | None = Field(default=None, ge=1)
    """Own CU slots used in parallel with this subscription; unset: overflow after its budget."""
    di_max_inflight: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _has_endpoint(self) -> Self:
        if not (self.di_endpoint or self.cu_endpoint):
            raise ValueError(f"Account {self.name} needs a DI or CU endpoint")
        return self


class BillingSettings(BaseModel):
    """USD budgets per billing account, filled in order: this subscription, then ``accounts``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_usd: float | None = Field(default=None, ge=0)
    """Budget of this subscription's DI and CU resources; unset means they take all the work."""
    accounts: tuple[ExternalAccount, ...] = ()
    di_page_usd: float | None = Field(default=None, ge=0)
    cu_page_estimate_usd: float | None = Field(default=None, ge=0)
    """Reserved per in-flight CU page until the operation reports its usage."""
    cu_meter_usd: dict[str, float] = Field(default_factory=dict)
    """USD per unit of each CU usage meter, e.g. ``gpt-5.4-mini-output``."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [account.name for account in self.accounts]
        if len(set(names)) != len(names):
            raise ValueError("Billing account names must be unique")
        if self.accounts and self.budget_usd is None:
            raise ValueError("External accounts are only used once budget_usd is reached")
        budgets = [self.budget_usd, *(account.budget_usd for account in self.accounts)]
        priced = self.di_page_usd is not None and self.cu_page_estimate_usd is not None
        if any(budget is not None for budget in budgets) and not (priced and self.cu_meter_usd):
            raise ValueError("USD budgets need di_page_usd, cu_page_estimate_usd and cu_meter_usd")
        return self


def load_billing(path: Path | None) -> BillingSettings:
    if path is None:
        return BillingSettings()
    return BillingSettings.model_validate_json(path.read_text(encoding="utf-8"))


class ParserSettings(BaseModel):
    work_dir: Path | None = None
    """Bounded scratch volume (emptyDir with sizeLimit in Kubernetes)."""
    include_hidden_sheets: bool = True
    max_cells: int = Field(default=2_000_000, ge=1)
    max_text_bytes: int = Field(default=64 * MIB, gt=0)
    zip_max_entries: int = Field(default=20_000, ge=1)
    zip_max_uncompressed_bytes: int = Field(default=1024 * MIB, gt=0)
    zip_max_ratio: float = Field(default=200.0, gt=1)
    dwg_timeout_seconds: float = Field(default=60.0, gt=0)
    dwg_memory_bytes: int = Field(default=1536 * MIB, ge=256 * MIB)


class ChunkSettings(BaseModel):
    min_tokens: int = Field(default=600, ge=1)
    max_tokens: int = Field(default=900, ge=64)
    overlap_tokens: int = Field(default=100, ge=0)
    table_max_tokens: int = Field(default=900, ge=64)
    allow_partial_projection: bool = False
    embed_batch_size: int = Field(default=64, ge=1)
    scientific_observations: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.min_tokens <= self.max_tokens or self.overlap_tokens >= self.min_tokens:
            raise ValueError("Chunk sizes need overlap < min_tokens <= max_tokens")
        return self


class TelemetrySettings(BaseModel):
    log_level: str = "INFO"
    json_logs: bool = True
    service_name: str = "crv-ingestion-pipeline"


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TIZ_", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    bus: BusSettings = Field(default_factory=BusSettings)
    blob: BlobSettings = Field(default_factory=BlobSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    di: DocumentIntelligenceSettings = Field(default_factory=DocumentIntelligenceSettings)
    cu: ContentUnderstandingSettings = Field(default_factory=ContentUnderstandingSettings)
    routing: RoutingPolicy = Field(
        default_factory=lambda: RoutingPolicy(
            version="crv-routing-v1", preserve_layout=True, prefer_native_text=False,
            independent_column_workbooks=frozenset({
                "84e42620e08e0bde04a1b34326abc115fc9a74b666c4a238c4a77ac8aaf96e97"
            }),
        )
    )
    routing_policy: str | None = None
    """Name of the registry policy that plans new snapshots (and so their search release)."""
    policies_path: Path | None = None
    """Optional ``PolicyRegistry`` JSON: extra routing policies and their search releases."""
    billing_path: Path | None = None
    """Optional ``BillingSettings`` JSON; unset keeps every paid analysis in this subscription."""
    parsers: ParserSettings = Field(default_factory=ParserSettings)
    chunking: ChunkSettings = Field(default_factory=ChunkSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @model_validator(mode="after")
    def _named_policy_needs_registry(self) -> Self:
        if self.routing_policy and self.policies_path is None:
            raise ValueError("routing_policy needs policies_path (TIZ_POLICIES_PATH)")
        return self

    def default_routing(self) -> RoutingPolicy:
        """The named registry policy when ``routing_policy`` is set, else ``routing``."""

        if not self.routing_policy:
            return self.routing
        return load_policies(self.policies_path).get(self.routing_policy).policy
