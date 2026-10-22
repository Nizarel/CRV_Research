from typing import Literal

from crv_agent.domain.evidence import Calculation, Claim, Evidence
from crv_agent.domain.planning import Contract


class Conflict(Contract):
    subject: str
    quantity: str
    claim_indices: list[int]


class AnswerDraft(Contract):
    content: str
    claims: list[Claim]
    insufficient: bool
    # Indices into claims; validated deterministically before reaching the answer.
    conflicts: list[Conflict] = []


class ComposedConflict(Contract):
    subject: str
    quantity: str
    claim_numbers: list[int]


class ComposedAnswer(Contract):
    """Composer output: answer text plus ledger claim numbers (claims are never re-typed)."""

    content: str
    claim_numbers: list[int]
    insufficient: bool
    conflicts: list[ComposedConflict]


class Verdict(Contract):
    supported: bool
    reasons: list[str]
    # Indices into draft.claims that the sources do not support (dropped before repair).
    unsupported_claims: list[int] = []
    # Verbatim content sentences that are unsupported (pruned if the final draft fails).
    unsupported_sentences: list[str] = []


class Coverage(Contract):
    matched: int = 0
    visited: int = 0
    enumerated: int = 0
    catalogue_complete: bool = False
    gaps: list[str] = []


class RoleUsage(Contract):
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    seconds: float
    models: dict[str, int]


class Answer(Contract):
    status: Literal[
        "answered", "partial", "insufficient_evidence", "needs_clarification", "out_of_scope",
    ]
    content: str
    claims: list[Claim]
    citations: list[Evidence]
    conflicts: list[Conflict]
    calculations: list[Calculation]
    coverage: Coverage
    reasons: list[str]
    search_release: str
    kg_release: str
    trace_id: str
    usage: dict[str, int]
    role_usage: dict[str, RoleUsage] = {}
    retrieval_mode: Literal["kg", "search"] = "kg"
