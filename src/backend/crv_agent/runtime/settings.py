from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from crv_agent.services.evidence_pack import EvidencePolicy

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
PgAuth = Literal["entra", "password"]
SslMode = Literal["disable", "prefer", "require", "verify-ca", "verify-full"]
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def endpoint_allowed(url: str, private_http_hosts: list[str], *, v1: bool = False) -> bool:
    """HTTPS anywhere; plain HTTP only to loopback or an explicitly listed private host."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    secure = parts.scheme == "https" or (
        parts.scheme == "http" and (parts.hostname or "") in (
            LOOPBACK_HOSTS | {host.lower() for host in private_http_hosts}
        )
    )
    return bool(
        secure and parts.hostname and parts.username is None and parts.password is None
        and not parts.query and not parts.fragment and (not v1 or not path or path.endswith("/v1")),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRV_", extra="ignore", hide_input_in_errors=True)

    mode: Literal["development"] = "development"
    retrieval_mode: Literal["kg", "search"] = "kg"
    # Required for search_backend=azure only.
    search_endpoint: str | None = None
    search_index: str = "crv-evidence-v1"
    search_release: str = "crv-v1"
    # azure: Azure AI Search; postgres: crv_retrieval in the KG database (ingestion migration 0022).
    search_backend: Literal["azure", "postgres"] = "azure"
    # Stored vector column searched by PostgreSQL: te3 (3072, Azure TE3) or m3 (1024, BGE-M3).
    pg_vector_model: Literal["te3", "m3"] = "te3"
    # Query embedder; vectors must come from the model that embedded the corpus column.
    # azure: Azure OpenAI deployment over Entra; openai: any OpenAI-compatible /v1 server.
    pg_embedder: Literal["azure", "openai"] = "azure"
    pg_embedding_deployment: str = "text-embedding-3-large"
    pg_embedding_url: str = ""
    pg_embedding_model: str = ""
    pg_embedding_api_key_file: str | None = None
    pg_search_pool_size: int = Field(default=10, ge=1, le=20)
    # Cross-encoder over the top 50 semantic results, in the semantic ranker's role.
    # cohere: Foundry Cohere v2 over Entra; vllm: Cohere-compatible /v1/rerank; tei: TEI /rerank.
    pg_reranker: Literal["none", "cohere", "vllm", "tei"] = "none"
    pg_reranker_url: str = ""
    pg_reranker_model: str = ""
    pg_reranker_api_key_file: str | None = None
    # Hosts allowed over plain HTTP besides loopback (private edge network only).
    private_http_hosts: list[str] = []
    kg_release: str
    kg_scopes: list[str] = ["crv:research", "crv:geometallurgy", "crv:environment"]
    postgres_host: str
    postgres_root_cert: str = "system"
    postgres_user: str
    # Defaults are the Azure ones; password auth reads the file on every new connection.
    postgres_port: int | None = Field(default=None, ge=1, le=65535)
    postgres_auth: PgAuth = "entra"
    postgres_sslmode: SslMode = "verify-full"
    postgres_password_file: str | None = None
    kg_database: str
    memory_database: str = "crv-agent-memory"
    memory_user: str
    # Unset memory_* connection values reuse the postgres_* ones (one server, as on Azure).
    memory_host: str | None = None
    memory_port: int | None = Field(default=None, ge=1, le=65535)
    memory_auth: PgAuth | None = None
    memory_sslmode: SslMode | None = None
    memory_root_cert: str | None = None
    memory_password_file: str | None = None
    # Azure OpenAI resource: chat when no model endpoint is set, and azure query embeddings.
    openai_endpoint: str | None = None
    model_endpoint: str | None = None
    # Unset: key when one is configured, otherwise Entra. none: unauthenticated local server.
    model_auth: Literal["entra", "key", "none"] | None = None
    model_api_key: SecretStr | None = None
    model_api_key_file: str | None = None
    planner_deployment: str
    worker_deployment: str
    answer_deployment: str
    verifier_deployment: str
    # Unset sends no reasoning_effort, so the deployment's default applies.
    planner_reasoning_effort: ReasoningEffort | None = None
    worker_reasoning_effort: ReasoningEffort | None = None
    answer_reasoning_effort: ReasoningEffort | None = None
    verifier_reasoning_effort: ReasoningEffort | None = None
    # Planner/answer/verifier cap; reasoning tokens count against it.
    output_tokens: int = Field(default=4096, ge=256, le=8192)
    worker_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    # extract: bounded retrieve-then-extract (default); agent: LangChain tool-loop worker.
    worker_mode: Literal["extract", "agent"] = "extract"
    # Claims kept per facet and extraction round (extract mode).
    worker_max_claims: int = Field(default=8, ge=1, le=24)
    # Parallel extraction calls per facet over round-robin passage batches (extract mode).
    extract_batches: int = Field(default=1, ge=1, le=4)
    # prune: after a failed verification, delete rejected text before composing a second draft.
    repair_mode: Literal["recompose", "prune"] = "recompose"
    # KG-2 pilot: released studies choose the documents of one strict search (needs 0021).
    kg_results: bool = False
    # Blob account/container of the ingestion source PDFs; unset disables GET /api/v1/sources.
    sources_account_url: str | None = None
    sources_container: str = "crv-sources"
    # Local alternative to Blob: <sources_root>/crv/sources/<sha256>.pdf.
    sources_root: str | None = None
    facet_concurrency: int = Field(default=4, ge=1, le=6)
    turn_tokens: int = Field(default=300_000, ge=8192, le=1_000_000)
    model_calls: int = Field(default=24, ge=4, le=40)
    turn_seconds: float = Field(default=120, ge=30, le=600)
    # SDK retries (Retry-After honoured) for 429/5xx, always bounded by the turn deadline.
    model_retries: int = Field(default=4, ge=0, le=6)
    # Extra attempts after an HTTP 404 from the model endpoint. The hosted NVIDIA catalog
    # intermittently answers 404 for models it serves; keep 0 for Azure and local servers.
    model_not_found_retries: int = Field(default=0, ge=0, le=4)
    evidence_pack_size: int = Field(default=50, ge=1, le=50)
    evidence_excerpt_characters: int = Field(default=8000, ge=200, le=8000)
    evidence_anchor: Literal["lexical", "caption"] = "lexical"
    evidence_rerank: Literal["none", "llm"] = "none"
    # Evaluation only: streams the full claim ledger and exposed passage text per turn.
    eval_diagnostics: bool = False
    local_owner: str | None = None
    tenant_id: str | None = None
    audience: str | None = None

    @field_validator(
        "planner_reasoning_effort", "worker_reasoning_effort", "answer_reasoning_effort",
        "verifier_reasoning_effort", mode="before",
    )
    @classmethod
    def blank_effort_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "search_endpoint", "openai_endpoint", "model_endpoint", "model_auth", "model_api_key_file",
        "pg_embedding_api_key_file", "pg_reranker_api_key_file", "sources_account_url",
        "sources_root", "postgres_port", "postgres_password_file", "memory_host", "memory_port",
        "memory_auth", "memory_sslmode", "memory_root_cert", "memory_password_file", mode="before",
    )
    @classmethod
    def blank_value_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def effective_model_auth(self) -> Literal["entra", "key", "none"]:
        if self.model_auth is not None:
            return self.model_auth
        return "key" if self.model_api_key or self.model_api_key_file else "entra"

    @model_validator(mode="after")
    def check_identity_and_isolation(self) -> "Settings":
        if not self.local_owner and not (self.tenant_id and self.audience):
            raise ValueError("Configure Entra identity or explicit local development owner")
        if self.eval_diagnostics and not self.local_owner:
            raise ValueError("Evaluation diagnostics are only allowed for loopback local owners")
        if self.kg_database == self.memory_database:
            raise ValueError("Agent memory must be separate from the ingestion database")
        self._check_databases()
        if not self.kg_scopes:
            raise ValueError("At least one KG navigation scope is required")
        for endpoint in (self.search_endpoint, self.openai_endpoint):
            if endpoint is not None and not endpoint.startswith("https://"):
                raise ValueError("Azure endpoints must use HTTPS")
        if self.search_backend == "azure" and not self.search_endpoint:
            raise ValueError("Azure AI Search needs a search endpoint")
        self._check_models()
        self._check_retrieval_models()
        if self.sources_account_url and not self.sources_account_url.startswith("https://"):
            raise ValueError("The sources account URL must use HTTPS")
        if self.sources_account_url and self.sources_root:
            raise ValueError("Configure Blob or local source documents, not both")
        return self

    def _check_databases(self) -> None:
        memory_auth = self.memory_auth or self.postgres_auth
        for auth, sslmode, password_file in (
            (self.postgres_auth, self.postgres_sslmode, self.postgres_password_file),
            (memory_auth, self.memory_sslmode or self.postgres_sslmode,
             self.memory_password_file or self.postgres_password_file),
        ):
            if auth == "password" and not password_file:
                raise ValueError("PostgreSQL password auth needs a password file")
            if auth == "entra" and sslmode != "verify-full":
                raise ValueError("Entra PostgreSQL logins require sslmode verify-full")

    def _check_models(self) -> None:
        auth = self.effective_model_auth
        if self.model_api_key is not None and self.model_api_key_file:
            raise ValueError("Configure a model API key or a key file, not both")
        if self.model_api_key is not None:
            if not self.model_endpoint or not self.model_api_key.get_secret_value().strip():
                raise ValueError(
                    "A model API key requires an explicit model endpoint and nonblank key",
                )
        if auth != "entra" and not self.model_endpoint:
            raise ValueError(
                "Key or unauthenticated model access requires an explicit model endpoint",
            )
        if auth == "key" and not (self.model_api_key or self.model_api_key_file):
            raise ValueError("Model auth 'key' needs a model API key or key file")
        if auth == "none" and (self.model_api_key or self.model_api_key_file):
            raise ValueError("Model auth 'none' must not configure an API key")
        if self.model_endpoint and not endpoint_allowed(
            self.model_endpoint, self.private_http_hosts if auth != "entra" else [], v1=True,
        ):
            raise ValueError(
                "Model endpoint must be an HTTPS origin or OpenAI v1 URL "
                "(HTTP only without Entra, to loopback or a private_http_hosts entry)",
            )
        if not self.model_endpoint and not self.openai_endpoint:
            raise ValueError("Chat models need a model endpoint or an Azure OpenAI endpoint")

    def _check_retrieval_models(self) -> None:
        if self.search_backend != "postgres":
            return
        if self.pg_embedder == "azure":
            if self.pg_vector_model != "te3":
                raise ValueError("Azure query embeddings only match the te3 vector column")
            if not self.openai_endpoint:
                raise ValueError("Azure query embeddings need an Azure OpenAI endpoint")
        elif not (
            self.pg_embedding_model
            and endpoint_allowed(self.pg_embedding_url, self.private_http_hosts, v1=True)
            and urlsplit(self.pg_embedding_url).path.rstrip("/").endswith("/v1")
        ):
            raise ValueError(
                "The openai embedder needs a /v1 URL (HTTPS, or HTTP to a private host) "
                "and a served model name",
            )
        if self.pg_reranker == "cohere" and not (
            self.pg_reranker_url.startswith("https://") and self.pg_reranker_model
        ):
            raise ValueError("The cohere reranker needs an HTTPS URL and a model")
        if self.pg_reranker in {"vllm", "tei"} and not endpoint_allowed(
            self.pg_reranker_url, self.private_http_hosts,
        ):
            raise ValueError("The local reranker needs an HTTPS URL or HTTP to a private host")
        if self.pg_reranker == "vllm" and not self.pg_reranker_model:
            raise ValueError("The vllm reranker needs a served model name")


def evidence_policy(settings: Settings) -> EvidencePolicy:
    return EvidencePolicy(
        pack_size=settings.evidence_pack_size,
        excerpt_characters=settings.evidence_excerpt_characters,
        anchor=settings.evidence_anchor, rerank=settings.evidence_rerank,
    )
