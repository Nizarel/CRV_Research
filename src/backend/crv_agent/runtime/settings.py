from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from crv_agent.services.evidence_pack import EvidencePolicy


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
    output_tokens: int = Field(default=2048, ge=256, le=8192)
    turn_tokens: int = Field(default=300_000, ge=8192, le=1_000_000)
    model_calls: int = Field(default=24, ge=4, le=40)
    evidence_pack_size: int = Field(default=16, ge=1, le=50)
    evidence_excerpt_characters: int = Field(default=1200, ge=200, le=8000)
    evidence_anchor: Literal["lexical", "caption"] = "lexical"
    evidence_rerank: Literal["none", "llm"] = "none"
    local_owner: str | None = None
    tenant_id: str | None = None
    audience: str | None = None

    @model_validator(mode="after")
    def check_identity_and_isolation(self) -> "Settings":
        if not self.local_owner and not (self.tenant_id and self.audience):
            raise ValueError("Configure Entra identity or explicit local development owner")
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
