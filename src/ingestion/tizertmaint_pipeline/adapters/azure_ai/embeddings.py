"""Token-aware, strictly reconciled embeddings over the Azure OpenAI API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from ...domain.batching import pack
from ...domain.errors import InvalidInputError, PipelineError, TransientError
from ...domain.vectors import Vector, reconcile_embeddings
from ..azure_errors import for_status, parse_retry_after

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def utf8_token_upper_bound(text: str) -> int:
    """Every BPE token covers at least one byte, so UTF-8 length bounds the token count."""

    return len(text.encode("utf-8"))


def translate_openai_error(error: Exception) -> PipelineError | None:
    import openai

    if isinstance(error, openai.APIStatusError):
        headers = dict(error.response.headers) if error.response is not None else None
        return for_status(
            error.status_code,
            f"Embeddings request failed with HTTP {error.status_code}",
            retry_after=parse_retry_after(headers),
        )
    if isinstance(error, openai.APIConnectionError):
        return TransientError(f"Embeddings transport failure: {type(error).__name__}")
    return None


class AzureOpenAIEmbeddings:
    """``EmbeddingProvider``: packs by tokens and count, validates every returned vector."""

    def __init__(
        self,
        client: Any,
        *,
        deployment: str,
        model: str,
        dimensions: int,
        max_inputs_per_request: int,
        max_tokens_per_request: int,
        max_tokens_per_input: int,
        concurrency: int = 2,
        token_counter: Callable[[str], int] = utf8_token_upper_bound,
    ) -> None:
        self._client = client
        self._deployment = deployment
        self._model = model
        self._dimensions = dimensions
        self._max_inputs = max_inputs_per_request
        self._max_request_tokens = max_tokens_per_request
        self._max_input_tokens = max_tokens_per_input
        self._semaphore = asyncio.Semaphore(concurrency)
        self._count_tokens = token_counter

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        if any(not text.strip() for text in texts):
            raise InvalidInputError("Embedding inputs must be non-empty")
        packed = pack(
            [self._count_tokens(text) for text in texts],
            max_items=self._max_inputs,
            max_total=self._max_request_tokens,
            max_item=self._max_input_tokens,
        )
        if packed.oversized:
            raise InvalidInputError(
                f"{len(packed.oversized)} input(s) exceed {self._max_input_tokens} tokens; "
                "split them before embedding"
            )
        results: list[Vector | None] = [None] * len(texts)

        async def run(batch: tuple[int, ...]) -> None:
            async with self._semaphore:
                vectors = await self._request([texts[index] for index in batch])
            for index, vector in zip(batch, vectors, strict=True):
                results[index] = vector

        try:
            async with asyncio.TaskGroup() as group:
                for batch in packed.batches:
                    group.create_task(run(batch))
        except ExceptionGroup as errors:
            # Surface one classified error; the TaskGroup already cancelled sibling batches.
            raise errors.exceptions[0] from errors
        return [vector for vector in results if vector is not None]

    async def _request(self, inputs: list[str]) -> list[Vector]:
        try:
            response = await self._client.embeddings.create(model=self._deployment, input=inputs)
        except Exception as error:
            raise (translate_openai_error(error) or error) from error
        return reconcile_embeddings(
            len(inputs),
            ((item.index, item.embedding) for item in response.data),
            dimensions=self._dimensions,
        )


def create_openai_client(endpoint: str, credential: Any, *, api_version: str) -> Any:
    """Async client with one SDK retry; the stage ledger owns the business retry budget."""

    from azure.identity.aio import get_bearer_token_provider
    from openai import AsyncAzureOpenAI

    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, COGNITIVE_SERVICES_SCOPE),
        api_version=api_version,
        max_retries=1,
    )
