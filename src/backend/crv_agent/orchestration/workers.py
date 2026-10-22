from typing import Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from crv_agent.domain.evidence import Evidence, WorkerResult
from crv_agent.domain.execution import Budget, BudgetExceeded, estimate_tokens
from crv_agent.orchestration.middleware import UsageRecorder
from crv_agent.orchestration.prompts import WORKER_RULES
from crv_agent.ports.rerank import Reranker
from crv_agent.services.evidence_pack import EvidencePolicy
from crv_agent.services.retrieval import Retrieval
from crv_agent.tools.evidence import evidence_tools


class Worker(Protocol):
    async def run(
        self, question: str, release: str, hashes: list[str], budget: Budget,
    ) -> tuple[WorkerResult, list[Evidence]]: ...


class WorkerBudget(AgentMiddleware[AgentState[WorkerResult], None]):
    def __init__(self, budget: Budget, output_tokens: int) -> None:
        self.budget, self.output_tokens = budget, output_tokens
        self.calls = 0

    async def abefore_model(
        self, state: AgentState[WorkerResult], runtime: Runtime[None],
    ) -> None:
        if self.calls >= 4:
            raise BudgetExceeded("Worker model call allowance exhausted")
        self.calls += 1
        await self.budget.reserve(
            model=1,
            tokens=estimate_tokens(
                "".join(str(message.content) for message in state["messages"]) + WORKER_RULES,
            ) + self.output_tokens + 1024,
        )


# Full-chunk tool results are large and are resent on every later model call. Above this
# approximate size only the newest tool results stay verbatim; the exposure ledger still
# records every shown span, so verification is unaffected.
CONTEXT_TRIGGER_TOKENS = 40_000
KEEP_TOOL_RESULTS = 2


class EvidenceWorker:
    def __init__(
        self, model: BaseChatModel, retrieval: Retrieval, output_tokens: int,
        policy: EvidencePolicy | None = None, reranker: Reranker | None = None,
    ) -> None:
        self.model, self.retrieval, self.output_tokens = model, retrieval, output_tokens
        self.policy, self.reranker = policy or EvidencePolicy(), reranker

    async def run(
        self, question: str, release: str, hashes: list[str], budget: Budget,
    ) -> tuple[WorkerResult, list[Evidence]]:
        found: dict[str, Evidence] = {}
        tools = evidence_tools(
            self.retrieval, release, hashes, budget, found, question, self.policy, self.reranker,
        )
        agent = create_agent(
            self.model, tools=tools, system_prompt=WORKER_RULES,
            response_format=WorkerResult,
            middleware=[
                ContextEditingMiddleware(edits=[ClearToolUsesEdit(
                    trigger=CONTEXT_TRIGGER_TOKENS, keep=KEEP_TOOL_RESULTS,
                    placeholder="[earlier tool result cleared to save context]",
                )]),
                WorkerBudget(budget, self.output_tokens),
            ],
        )
        output = await budget.wait(agent.ainvoke(
            {"messages": [HumanMessage(content=question)]},
            {"recursion_limit": 16, "callbacks": [UsageRecorder(budget, "evidence_worker")]},
        ))
        return WorkerResult.model_validate(output["structured_response"]), list(found.values())
