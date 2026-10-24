import json
import os
import re
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from crv_agent.domain.answers import AnswerDraft, ComposedAnswer, Conflict, Verdict
from crv_agent.domain.evidence import CalculationRequest, CandidateRanking, Claim, Evidence
from crv_agent.domain.execution import Budget, estimate_tokens
from crv_agent.domain.planning import QueryPlan
from crv_agent.orchestration.middleware import UsageRecorder
from crv_agent.orchestration.prompts import COMPOSE_RULES, PLAN_RULES, RERANK_RULES, VERIFY_RULES
from crv_agent.services.evidence_pack import quote_windows

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
        *, role: str,
    ) -> T:
        await budget.reserve(
            model=1, tokens=estimate_tokens(system + data) + self.output_tokens
            + estimate_tokens(json.dumps(schema.model_json_schema())),
        )
        result = await budget.wait(model.with_structured_output(
            schema, method="json_schema", strict=True,
        ).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=data)],
            {"callbacks": [UsageRecorder(budget, role, schema.__name__)]},
        ))
        return schema.model_validate(result)

    async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
        return await self._invoke(
            self.planner, QueryPlan, PLAN_RULES,
            json.dumps({"question": question, "history": history[-6:]}, ensure_ascii=False), budget,
            role="planner",
        )

    async def compose(
        self, question: str, claims: list[Claim], context: str, budget: Budget,
    ) -> AnswerDraft:
        # Short citation handles: long opaque evidence IDs are error-prone to copy.
        handles = {
            evidence_id: f"S{n}" for n, evidence_id in enumerate(
                dict.fromkeys(e for c in claims for e in c.evidence_ids), 1,
            )
        }
        composed = await self._invoke(
            self.answer, ComposedAnswer, COMPOSE_RULES,
            json.dumps({
                "question": question,
                "claims": [
                    {
                        "number": n, "cite": [handles[e] for e in c.evidence_ids],
                        **c.model_dump(mode="json", exclude_none=True, exclude={"evidence_ids"}),
                    } for n, c in enumerate(claims)
                ],
                "context": context,
            }, ensure_ascii=False), budget, role="answer",
        )
        evidence_ids = {handle: evidence_id for evidence_id, handle in handles.items()}
        content = re.sub(
            r"\[(S\d+)\]", lambda m: f"[{evidence_ids.get(m.group(1), m.group(1))}]",
            composed.content,
        )
        # Unknown numbers are dropped here; deterministic verification then rejects any
        # citation marker or number not backed by a selected ledger claim.
        selected = [
            claims[n] for n in dict.fromkeys(composed.claim_numbers) if 0 <= n < len(claims)
        ]
        position = {id(claim): n for n, claim in enumerate(selected)}
        conflicts = []
        for item in composed.conflicts:
            indices = sorted({
                position[id(claims[n])] for n in item.claim_numbers
                if 0 <= n < len(claims) and id(claims[n]) in position
            })
            # A conflict needs two selected claims that actually differ.
            if len(indices) >= 2 and len({
                (selected[i].value, selected[i].unit, selected[i].quote) for i in indices
            }) >= 2:
                conflicts.append(Conflict(
                    subject=item.subject, quantity=item.quantity, claim_indices=indices,
                ))
        return AnswerDraft(
            content=content, claims=selected, insufficient=composed.insufficient,
            conflicts=conflicts, unanswered=composed.unanswered,
        )

    async def verify(
        self, draft: AnswerDraft, evidence: list[Evidence], context: str, budget: Budget,
    ) -> Verdict:
        return await self._invoke(
            self.verifier, Verdict, VERIFY_RULES,
            json.dumps({
                "draft": draft.model_dump(mode="json"),
                # Focused windows around each claim quote: enough to check bindings without
                # resending whole chunks (the deterministic contract checks quotes exactly).
                "sources": [
                    {
                        **e.model_dump(
                            mode="json", exclude={"content", "exposed", "caption"},
                        ),
                        "shown_text": quote_windows(e, [
                            c.quote for c in draft.claims if e.id in c.evidence_ids
                        ]),
                    } for e in evidence
                ],
                "calculation_and_coverage_context": context,
            }, ensure_ascii=False), budget, role="verifier",
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
            budget, role="planner",
        )


class LangChainReranker:
    """Listwise reranker over candidate previews; returns IDs only, never evidence text."""

    def __init__(self, models: LangChainModels, model: BaseChatModel) -> None:
        self.models, self.model = models, model

    async def rerank(
        self, query: str, candidates: list[tuple[str, str, str]], budget: Budget,
    ) -> list[str]:
        ranking = await self.models._invoke(
            self.model, CandidateRanking, RERANK_RULES,
            json.dumps({
                "query": query,
                "candidates": [
                    {"id": key, "title": title, "preview": preview}
                    for key, title, preview in candidates
                ],
            }, ensure_ascii=False), budget, role="worker",
        )
        return ranking.ids
