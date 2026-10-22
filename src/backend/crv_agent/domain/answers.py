from typing import Literal

from crv_agent.domain.evidence import Calculation, Claim, Evidence
from crv_agent.domain.planning import Contract


class AnswerDraft(Contract):
    content: str
    claims: list[Claim]
    insufficient: bool


class Verdict(Contract):
    supported: bool
    reasons: list[str]


class Conflict(Contract):
    subject: str
    quantity: str
    claim_indices: list[int]


class Coverage(Contract):
    matched: int = 0
    visited: int = 0
    enumerated: int = 0
    catalogue_complete: bool = False
    gaps: list[str] = []


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
    retrieval_mode: Literal["kg", "search"] = "kg"
