from typing import Protocol

from crv_agent.domain.answers import AnswerDraft, Verdict
from crv_agent.domain.evidence import CalculationRequest, Claim, Evidence
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import QueryPlan


class Models(Protocol):
    async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan: ...

    async def compose(
        self, question: str, claims: list[Claim], context: str, budget: Budget,
    ) -> AnswerDraft: ...

    async def verify(
        self, draft: AnswerDraft, evidence: list[Evidence], context: str, budget: Budget,
    ) -> Verdict: ...

    async def calculation(
        self, question: str, claims: list[Claim], budget: Budget,
    ) -> CalculationRequest: ...
