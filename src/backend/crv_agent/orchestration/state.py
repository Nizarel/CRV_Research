from typing import Any, TypedDict

from crv_agent.domain.answers import Answer, Conflict, Coverage
from crv_agent.domain.evidence import Calculation, Claim, Evidence
from crv_agent.domain.planning import QueryPlan


class ResearchInput(TypedDict):
    question: str
    history: list[str]


class ResearchState(ResearchInput):
    plan: QueryPlan
    evidence: list[Evidence]
    claims: list[Claim]
    conflicts: list[Conflict]
    calculations: list[Calculation]
    coverage: Coverage
    answer: Answer
    # Evaluation diagnostics only (see ResearchContext.diagnostics).
    audit: list[dict[str, Any]]
