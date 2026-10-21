from decimal import Decimal
from typing import Literal

from crv_agent.domain.planning import Contract


class Evidence(Contract):
    id: str
    release: str
    chunk_id: str
    content_hash: str
    title: str
    content: str
    source_uri: str
    pages: list[int]
    locators: list[str]
    original_sources: list[str]
    generated: bool
    document_class: str
    citation_basis: str = "source"
    extraction_route: str = "unknown"
    original_content_hashes: list[str] = []
    source_role: Literal["unclassified", "secondary_summary"] = "unclassified"
    source_role_basis: Literal["filename"] | None = None


class Claim(Contract):
    text: str
    evidence_ids: list[str]
    quote: str
    subject: str
    quantity: str | None
    value: Decimal | None
    unit: str | None
    date: str | None
    conditions: str | None
    scope: str | None
    calculation_index: int | None = None
    qualifier: Literal["<", "<=", ">", ">=", "~"] | None = None


class WorkerResult(Contract):
    claims: list[Claim]
    gaps: list[str]


class CalculationRequest(Contract):
    operation: str
    input_claims: list[int]
    target_unit: str | None


class Calculation(Contract):
    operation: str
    input_claims: list[int]
    value: Decimal
    unit: str
    evidence_ids: list[str]
