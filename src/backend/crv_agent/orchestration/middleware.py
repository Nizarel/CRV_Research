from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry import trace

from crv_agent.domain.execution import Budget


class UsageRecorder(AsyncCallbackHandler):
    raise_error = True

    def __init__(self, budget: Budget, role: str) -> None:
        self.budget, self.role = budget, role

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if isinstance(message, AIMessage) and message.usage_metadata:
                    usage = message.usage_metadata
                    await self.budget.record_tokens(usage["input_tokens"], usage["output_tokens"])
                    span = trace.get_current_span()
                    span.add_event("model_usage", {
                        "crv.model_role": self.role,
                        "gen_ai.usage.input_tokens": usage["input_tokens"],
                        "gen_ai.usage.output_tokens": usage["output_tokens"],
                    })
