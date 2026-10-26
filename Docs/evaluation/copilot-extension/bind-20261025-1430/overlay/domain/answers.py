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
    # Parts the user explicitly asked for that the selected claims do not answer.
    unanswered: list[str] = []


class ComposedConflict(Contract):
    subject: str
    quantity: str
    claim_numbers: list[int]


class ComposedAnswer(Contract):
    """Composer output: answer text plus ledger claim numbers (claims are never re-typed)."""

    content: str
    claim_numbers: list[int]
    insufficient: bool
    unanswered: list[str]
    conflicts: list[ComposedConflict]


class BindingIssue(Contract):
    claim: int
    fields: list[Literal["date", "conditions", "scope"]]


class Verdict(Contract):
    supported: bool
    reasons: list[str]
    # Indices into draft.claims that the sources do not support (dropped before repair).
    unsupported_claims: list[int] = []
    # Otherwise supported claims whose listed optional bindings are cleared, not dropped.
    unsupported_bindings: list[BindingIssue] = []
    # Verbatim content sentences that are unsupported (pruned if the final draft fails).
    unsupported_sentences: list[str] = []


class Coverage(Contract):
    matched: int = 0
    visited: int = 0
    enumerated: int = 0
    catalogue_complete: bool = False
    # Research was cut short (a facet failed or ran out of budget, or a listing was truncated).
    incomplete: bool = False
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
