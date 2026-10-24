from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from crv_agent.services.evidence_pack import EvidencePolicy

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRV_", extra="ignore")

    mode: Literal["development"] = "development"
    retrieval_mode: Literal["kg", "search"] = "kg"
    search_endpoint: str
    search_index: str = "crv-evidence-v1"
    search_release: str = "crv-v1"
    kg_release: str
    kg_scopes: list[str] = ["crv:research", "crv:geometallurgy", "crv:environment"]
    postgres_host: str
    postgres_root_cert: str = "system"
    postgres_user: str
    kg_database: str
    memory_database: str = "crv-agent-memory"
    memory_user: str
    openai_endpoint: str
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
    facet_concurrency: int = Field(default=4, ge=1, le=6)
    turn_tokens: int = Field(default=300_000, ge=8192, le=1_000_000)
    model_calls: int = Field(default=24, ge=4, le=40)
    turn_seconds: float = Field(default=120, ge=30, le=600)
    # SDK retries (Retry-After honoured) for 429/5xx, always bounded by the turn deadline.
    model_retries: int = Field(default=4, ge=0, le=6)
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

    @model_validator(mode="after")
    def check_identity_and_isolation(self) -> "Settings":
        if not self.local_owner and not (self.tenant_id and self.audience):
            raise ValueError("Configure Entra identity or explicit local development owner")
        if self.eval_diagnostics and not self.local_owner:
            raise ValueError("Evaluation diagnostics are only allowed for loopback local owners")
        if self.kg_database == self.memory_database:
            raise ValueError("Agent memory must be separate from the ingestion database")
        if not self.kg_scopes:
            raise ValueError("At least one KG navigation scope is required")
        if not self.search_endpoint.startswith("https://") or not self.openai_endpoint.startswith(
            "https://",
        ):
            raise ValueError("Azure endpoints must use HTTPS")
        return self


def evidence_policy(settings: Settings) -> EvidencePolicy:
    return EvidencePolicy(
        pack_size=settings.evidence_pack_size,
        excerpt_characters=settings.evidence_excerpt_characters,
        anchor=settings.evidence_anchor, rerank=settings.evidence_rerank,
    )
