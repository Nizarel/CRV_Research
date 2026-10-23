import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import WithJsonSchema, field_validator

from crv_agent.domain.planning import Contract

_DECIMAL = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
# Strict structured outputs reject Pydantic's Decimal pattern (regex lookaround), so the
# published schema is a plain union; numeric_value_or_none parses and normalizes.
ModelDecimal = Annotated[
    Decimal | None,
    WithJsonSchema({"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]}),
]


class Evidence(Contract):
    id: str
    release: str
    chunk_id: str
    content_hash: str
    title: str
    content: str
    source_uri: str
    # Original corpus path (source_uri is content-addressed); used for display and genre.
    source_path: str = ""
    document_code: str | None = None
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
    # Semantic-ranker extractive caption (derived, never quotable; used only to place excerpts).
    caption: str | None = None
    # Character spans shown to a model; None means the complete content was supplied.
    exposed: list[tuple[int, int]] | None = None


class Claim(Contract):
    text: str
    evidence_ids: list[str]
    quote: str
    subject: str
    quantity: str | None
    value: ModelDecimal
    unit: str | None
    date: str | None
    conditions: str | None
    scope: str | None
    calculation_index: int | None = None
    qualifier: Literal["<", "<=", ">", ">=", "~"] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value_or_none(cls, value: object) -> object:
        """Accept decimal commas; ranges and words stay qualitative (value None, exact quote).

        One unparsable value must not invalidate a worker's whole structured result.
        """
        if not isinstance(value, str):
            return value
        text = value.strip().replace("\u202f", "").replace("\u00a0", "").replace(" ", "")
        if _DECIMAL.fullmatch(text):
            return text.replace(",", ".")
        return None


class WorkerResult(Contract):
    claims: list[Claim]
    gaps: list[str]


class ExtractionRound(Contract):
    """One extraction round, traced for evaluation diagnostics (never a model schema)."""

    query: str
    passage_ids: list[str]
    returned: list[Claim]
    capped: list[Claim]
    gaps: list[str]
    rewrite: str | None


class TracedWorkerResult(WorkerResult):
    rounds: list[ExtractionRound] = []


class CandidateRanking(Contract):
    """Listwise rerank output: candidate IDs, most useful first."""

    ids: list[str]


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
