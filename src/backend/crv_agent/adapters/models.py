import json
import os
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from crv_agent.domain.answers import AnswerDraft, Verdict
from crv_agent.domain.evidence import CalculationRequest, Claim, Evidence
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import QueryPlan
from crv_agent.orchestration.middleware import UsageRecorder
from crv_agent.orchestration.prompts import COMPOSE_RULES, PLAN_RULES, VERIFY_RULES

T = TypeVar("T", bound=BaseModel)

def reject_unapproved_tracing() -> None:
    if any(
        os.environ.get(flag, "").lower() in {"1", "true", "yes"}
        for flag in (
            "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING",
            "LANGSMITH_OTEL_ENABLED",
        )
    ):
        raise RuntimeError("Automatic LangSmith/content tracing is not approved for this corpus")


class LangChainModels:
    def __init__(
        self, planner: BaseChatModel, answer: BaseChatModel, verifier: BaseChatModel,
        output_tokens: int,
    ) -> None:
        self.planner, self.answer, self.verifier = planner, answer, verifier
        self.output_tokens = output_tokens

    async def _invoke(
        self, model: BaseChatModel, schema: type[T], system: str, data: str, budget: Budget,
    ) -> T:
        # UTF-8 bytes are a conservative upper bound for byte-pair input tokenization.
        await budget.reserve(
            model=1, tokens=len((system + data).encode()) + self.output_tokens
            + len(json.dumps(schema.model_json_schema()).encode()),
        )
        result = await budget.wait(model.with_structured_output(
            schema, method="json_schema", strict=True,
        ).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=data)],
            {"callbacks": [UsageRecorder(budget, schema.__name__)]},
        ))
        return schema.model_validate(result)

    async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
        return await self._invoke(
            self.planner, QueryPlan, PLAN_RULES,
            json.dumps({"question": question, "history": history[-6:]}, ensure_ascii=False), budget,
        )

    async def compose(
        self, question: str, claims: list[Claim], context: str, budget: Budget,
    ) -> AnswerDraft:
        return await self._invoke(
            self.answer, AnswerDraft, COMPOSE_RULES,
            json.dumps({
                "question": question, "claims": [c.model_dump(mode="json") for c in claims],
                "context": context,
            }, ensure_ascii=False), budget,
        )

    async def verify(
        self, draft: AnswerDraft, evidence: list[Evidence], context: str, budget: Budget,
    ) -> Verdict:
        return await self._invoke(
            self.verifier, Verdict, VERIFY_RULES,
            json.dumps({
                "draft": draft.model_dump(mode="json"),
                "sources": [e.model_dump(mode="json") for e in evidence],
                "calculation_and_coverage_context": context,
            }, ensure_ascii=False), budget,
        )

    async def calculation(
        self, question: str, claims: list[Claim], budget: Budget,
    ) -> CalculationRequest:
        return await self._invoke(
            self.planner, CalculationRequest,
            "Select a typed calculation on cited claim indices (zero-based). Operations: "
            "convert, ratio, recovery (numerator/denominator * 100), sum, "
            "weighted_recovery (concentrate mass, concentrate grade, feed mass, feed grade). "
            "Do not invent inputs. Ambiguous or unsupported computations use operation='unknown'.",
            json.dumps({
                "question": question, "claims": [c.model_dump(mode="json") for c in claims],
            }),
            budget,
        )
