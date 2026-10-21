from typing import Protocol

from crv_agent.domain.execution import Budget


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: list[tuple[str, str, str]], budget: Budget,
    ) -> list[str]:
        """Return candidate IDs, most useful first, from (id, title, preview) triples."""
        ...
