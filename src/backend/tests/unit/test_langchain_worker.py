import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from crv_agent.domain.evidence import Evidence, WorkerResult
from crv_agent.domain.execution import Budget
from crv_agent.orchestration.workers import EvidenceWorker
from crv_agent.services.retrieval import Retrieval
from tests.fakes import CLAIM, SOURCE


class ToolModel(BaseChatModel):
    _calls: int = PrivateAttr(default=0)
    deep_read: bool = False

    @property
    def _llm_type(self) -> str:
        return "test-tools"

    def bind_tools(
        self, tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *, tool_choice: str | None = None, **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        self._calls += 1
        if self._calls == 1:
            name, args = "search_evidence", {"query": "Recovery?"}
        elif self.deep_read and self._calls == 2:
            assert isinstance(messages[-1].content, str)
            listing = json.loads(messages[-1].content)
            assert len(listing["candidates"]) == 50
            assert listing["candidates"][-1]["id"] == "S49"
            name, args = "read_evidence", {"evidence_ids": ["S49"]}
        else:
            selected = CLAIM
            if self.deep_read:
                assert isinstance(messages[-1].content, str)
                read = json.loads(messages[-1].content)
                assert read[0]["id"] == "S49" and SOURCE.content in read[0]["content"]
                selected = CLAIM.model_copy(update={"evidence_ids": ["S49"]})
            name, args = "WorkerResult", WorkerResult(
                claims=[selected], gaps=[],
            ).model_dump(mode="json")
        call = {
            "name": name, "args": args,
            "id": f"call-{self._calls}", "type": "tool_call",
        }
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="", tool_calls=[call],
        ))])


@pytest.mark.asyncio
async def test_real_create_agent_search_and_structured_result() -> None:
    class Search:
        async def query(
            self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        ) -> list[Evidence]:
            return [SOURCE]

        async def ready(self) -> None:
            return None

    budget = Budget()
    worker = EvidenceWorker(ToolModel(), Retrieval(Search()), 1024)
    result, evidence = await worker.run("Recovery?", "r1", [], budget)
    assert result.claims == [CLAIM]
    assert evidence == [SOURCE]
    assert budget.model_calls == 2
    assert budget.retrieval_calls == 1


@pytest.mark.asyncio
async def test_real_create_agent_reads_a_deep_candidate_before_claiming() -> None:
    class Search:
        async def query(
            self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        ) -> list[Evidence]:
            assert mode == "semantic" and top == 50
            return [
                SOURCE.model_copy(update={
                    "id": f"S{i}", "chunk_id": f"c{i}", "content_hash": f"{i:064x}",
                    "content": "x" * 3000 + SOURCE.content,
                }) for i in range(50)
            ]

        async def ready(self) -> None:
            return None

    budget = Budget()
    worker = EvidenceWorker(ToolModel(deep_read=True), Retrieval(Search()), 1024)
    result, evidence = await worker.run("Recovery?", "r1", [], budget)
    assert result.claims[0].evidence_ids == ["S49"]
    assert SOURCE.content in next(hit.content for hit in evidence if hit.id == "S49")
    assert budget.model_calls == 3
    assert budget.retrieval_calls == 1
