"""Query-side models for PostgreSQL search: what Azure AI Search does server-side (the index's
query vectorizer and the semantic ranker), over Azure (Entra) or local OpenAI-compatible HTTP."""

import asyncio
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

Token = Callable[[], Awaitable[str]]
EMBEDDING_API_VERSION = "2024-10-21"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def static_token(value: str) -> Token:
    async def token() -> str:
        return value

    return token


def _retry_after(headers: Any, attempt: int) -> float:
    try:
        return min(float(headers.get("Retry-After", 2**attempt)), 10.0)
    except ValueError:
        return float(2**attempt)


async def post_json(
    session: aiohttp.ClientSession, url: str, body: dict[str, Any], token: Token | None, *,
    attempts: int = 3,
) -> Any:
    """POST (with a bearer token when given); retry throttling and transient errors (bounded)."""
    for attempt in range(attempts):
        headers = {"Authorization": f"Bearer {await token()}"} if token is not None else {}
        async with session.post(url, json=body, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            if response.status not in _RETRY_STATUS or attempt == attempts - 1:
                detail = (await response.text())[:300]
                raise RuntimeError(f"HTTP {response.status} from {url.split('?')[0]}: {detail}")
            delay = _retry_after(response.headers, attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("No attempt was made")


class _CachedEmbedder:
    """One call per distinct text, shared by the concurrent search legs of a question."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, token: Token | None, *,
        dimensions: int, cache_size: int = 256,
    ) -> None:
        self.session = session
        self.url = url
        self.token = token
        self.dimensions = dimensions
        self.cache_size = cache_size
        self.cache: OrderedDict[str, asyncio.Task[list[float]]] = OrderedDict()

    def body(self, text: str) -> dict[str, Any]:
        return {"input": [text]}

    async def embed(self, text: str) -> list[float]:
        task = self.cache.get(text)
        if task is None:
            task = asyncio.create_task(self._fetch(text))
            self.cache[text] = task
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(text)
        try:
            return await asyncio.shield(task)
        except Exception:
            if self.cache.get(text) is task:
                del self.cache[text]
            raise

    async def _fetch(self, text: str) -> list[float]:
        payload = await post_json(self.session, self.url, self.body(text), self.token)
        data = payload.get("data") if isinstance(payload, dict) else None
        vector = data[0].get("embedding") if isinstance(data, list) and data else None
        if not isinstance(vector, list) or len(vector) != self.dimensions or not all(
            isinstance(value, int | float) and math.isfinite(value) for value in vector
        ):
            raise ValueError("Embedding response has an unexpected shape")
        return [float(value) for value in vector]


class AzureOpenAIQueryEmbedder(_CachedEmbedder):
    """Query vectors from the Azure deployment that embedded the corpus (Entra token)."""

    def __init__(
        self, session: aiohttp.ClientSession, endpoint: str, deployment: str, token: Token, *,
        dimensions: int = 3072, cache_size: int = 256,
    ) -> None:
        super().__init__(
            session,
            f"{endpoint.rstrip('/')}/openai/deployments/{quote(deployment, safe='')}"
            f"/embeddings?api-version={EMBEDDING_API_VERSION}",
            token, dimensions=dimensions, cache_size=cache_size,
        )


class OpenAICompatibleQueryEmbedder(_CachedEmbedder):
    """Query vectors from an OpenAI-compatible server (vLLM, TEI) given as its /v1 base URL."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, model: str, token: Token | None, *,
        dimensions: int, cache_size: int = 256,
    ) -> None:
        super().__init__(
            session, f"{base_url.rstrip('/')}/embeddings", token,
            dimensions=dimensions, cache_size=cache_size,
        )
        self.model = model

    def body(self, text: str) -> dict[str, Any]:
        return {"model": self.model, "input": [text]}


def _checked_order(order: list[int], count: int) -> list[int]:
    if len(set(order)) != len(order) or any(not 0 <= i < count for i in order):
        raise ValueError("Rerank response has an unexpected shape")
    return order


class CohereReranker:
    """Cohere v2 rerank request shape: Cohere in Foundry, or vLLM's /v1/rerank."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, model: str, token: Token | None,
    ) -> None:
        self.session = session
        self.url = url
        self.model = model
        self.token = token

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        payload = await post_json(self.session, self.url, {
            "model": self.model, "query": query, "documents": documents,
            "top_n": len(documents),
        }, self.token)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ValueError("Rerank response has no results")
        ranked = sorted(results, key=lambda item: -float(item["relevance_score"]))
        return _checked_order([int(item["index"]) for item in ranked], len(documents))


class TeiReranker:
    """Hugging Face Text Embeddings Inference ``/rerank``: a list of index/score pairs."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: Token | None) -> None:
        self.session = session
        self.url = url
        self.token = token

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        payload = await post_json(self.session, self.url, {
            "query": query, "texts": documents, "truncate": True,
        }, self.token)
        if not isinstance(payload, list):
            raise ValueError("Rerank response has no results")
        ranked = sorted(payload, key=lambda item: -float(item["score"]))
        return _checked_order([int(item["index"]) for item in ranked], len(documents))
