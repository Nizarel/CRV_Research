import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry import trace

from crv_agent.domain.execution import Budget


class UsageRecorder(AsyncCallbackHandler):
    """Records provider-reported usage per deployment role (planner/worker/answer/verifier)."""

    raise_error = True

    def __init__(self, budget: Budget, role: str, purpose: str | None = None) -> None:
        self.budget, self.role, self.purpose = budget, role, purpose or role
        self.started: dict[UUID, float] = {}

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *, run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.started[run_id] = time.monotonic()

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started = self.started.pop(run_id, None)
        seconds = time.monotonic() - started if started is not None else 0.0
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if isinstance(message, AIMessage) and message.usage_metadata:
                    usage = message.usage_metadata
                    model = str(message.response_metadata.get("model_name") or "")
                    cached = usage.get("input_token_details", {}).get("cache_read") or 0
                    reasoning = usage.get("output_token_details", {}).get("reasoning") or 0
                    await self.budget.record_tokens(
                        usage["input_tokens"], usage["output_tokens"], role=self.role,
                        model=model, cached=cached, reasoning=reasoning, seconds=seconds,
                    )
                    seconds = 0.0
                    span = trace.get_current_span()
                    span.add_event("model_usage", {
                        "crv.model_role": self.purpose,
                        "crv.deployment_role": self.role,
                        "gen_ai.response.model": model,
                        "gen_ai.usage.input_tokens": usage["input_tokens"],
                        "gen_ai.usage.output_tokens": usage["output_tokens"],
                        "crv.usage.cached_tokens": cached,
                        "crv.usage.reasoning_tokens": reasoning,
                    })
