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
from crv_agent.services.evidence_pack import exposed_text, quote_windows

T = TypeVar("T", bound=BaseModel)
# A claim citation: [C3], or a list [C3, C7] / [C3; 7].
_CLAIM_CITE = re.compile(r"\[\s*[Cc]\s*(\d+(?:\s*[,;]\s*[Cc]?\s*\d+)*)\s*\]")
_MARKER_RUN = re.compile(r"(?:\[[^\[\]\n]+\])+")

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
        composed = await self._invoke(
            self.answer, ComposedAnswer, COMPOSE_RULES,
            json.dumps({
                "question": question,
                "claims": [
                    {
                        "number": n,
                        **c.model_dump(mode="json", exclude_none=True, exclude={"evidence_ids"}),
                    } for n, c in enumerate(claims)
                ],
                "context": context,
            }, ensure_ascii=False), budget, role="answer",
        )
        # The composer cites claims ([C3]); each becomes its claim's source markers, so the
        # cited claims are the selected ones and no source handle is ever copied by a model.
        cited: list[int] = []

        def expand(match: re.Match[str]) -> str:
            numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
            if not all(0 <= n < len(claims) for n in numbers):
                return match.group(0)
            cited.extend(numbers)
            return "".join(f"[{e}]" for n in numbers for e in claims[n].evidence_ids)

        content = _MARKER_RUN.sub(
            lambda run: "".join(dict.fromkeys(re.findall(r"\[[^\[\]]+\]", run.group(0)))),
            _CLAIM_CITE.sub(expand, composed.content),
        )
        markers = set(re.findall(r"\[([^\[\]]+)\]", content))
        # Listed but uncited claims are kept only when their sources are visibly cited.
        listed = [
            n for n in composed.claim_numbers
            if 0 <= n < len(claims) and set(claims[n].evidence_ids) <= markers
        ]
        selected = [claims[n] for n in dict.fromkeys(cited + listed)]
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
        def shown(item: Evidence) -> list[str]:
            citing = [c for c in draft.claims if item.id in c.evidence_ids]
            # A passage cited as context for a quote it does not hold is shown whole.
            if any(c.calculation_index is None and c.quote not in item.content for c in citing):
                return exposed_text(item)
            return quote_windows(item, [c.quote for c in citing])

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
                        "shown_text": shown(e),
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
            "serial_recovery (first-stage recovery, downstream recovery of remaining metal); "
            "percentage_point_difference (first recovery minus second recovery). "
            "Recovery inputs must be cited percentages or fractions, not invented constants. "
            "For serial_recovery, set comparison_claim to the separate cited observed/global "
            "actual recovery index when supplied, so its difference is also computed; "
            "otherwise null. "
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
