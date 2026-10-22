import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


def estimate_tokens(text: str) -> int:
    """Pre-call estimate; provider-reported usage enforces the hard turn limit."""
    return len(text.encode()) // 3 + 1


class BudgetExceeded(RuntimeError):
    pass


class DependencyUnavailable(RuntimeError):
    pass


@dataclass
class Budget:
    retrieval_limit: int = 24
    model_limit: int = 24
    token_limit: int = 300_000
    seconds: float = 120
    retrieval_calls: int = 0
    model_calls: int = 0
    reserved_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def remaining(self) -> float:
        return max(0, self.seconds - (time.monotonic() - self.started))

    async def reserve(self, *, retrieval: int = 0, model: int = 0, tokens: int = 0) -> None:
        async with self._lock:
            if (
                self.remaining <= 0
                or self.retrieval_calls + retrieval > self.retrieval_limit
                or self.model_calls + model > self.model_limit
                or self.reserved_tokens + tokens > self.token_limit
            ):
                raise BudgetExceeded("Turn execution budget exhausted")
            self.retrieval_calls += retrieval
            self.model_calls += model
            self.reserved_tokens += tokens

    async def wait(self, work: Awaitable[T]) -> T:
        return await asyncio.wait_for(work, timeout=self.remaining)

    async def record_tokens(self, inputs: int, outputs: int) -> None:
        async with self._lock:
            self.input_tokens += inputs
            self.output_tokens += outputs
            if self.input_tokens + self.output_tokens > self.token_limit:
                raise BudgetExceeded("Provider-reported token usage exceeded the turn budget")

    def usage(self) -> dict[str, int]:
        return {
            "retrieval_calls": self.retrieval_calls,
            "model_calls": self.model_calls,
            "reserved_tokens": self.reserved_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
