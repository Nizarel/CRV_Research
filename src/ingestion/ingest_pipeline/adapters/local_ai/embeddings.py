"""``EmbeddingProvider`` over an OpenAI-compatible ``/v1/embeddings`` endpoint (vLLM, TEI).

Used on the edge for BGE-M3, which replaces the Azure TE3 deployment. Every response is
reconciled strictly, as for Azure, so a partial answer is never stored as a vector.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...domain.errors import (
    ConfigurationError,
    IntegrityError,
    InvalidInputError,
    PipelineError,
    ThrottledError,
    TransientError,
)
from ...domain.vectors import Vector, reconcile_embeddings
from ..azure_errors import for_status, parse_retry_after

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_endpoint(url: str, private_http_hosts: Sequence[str]) -> str:
    """The ``/v1`` base URL: HTTPS anywhere, plain HTTP only to loopback or a listed host."""

    parts = urlsplit(url)
    allowed_http = LOOPBACK_HOSTS | {host.lower() for host in private_http_hosts}
    secure = parts.scheme == "https" or (
        parts.scheme == "http" and (parts.hostname or "") in allowed_http
    )
    if not (
        secure and parts.hostname and parts.username is None and parts.password is None
        and not parts.query and not parts.fragment and parts.path.rstrip("/").endswith("/v1")
    ):
        raise ConfigurationError(
            "Local embeddings need an HTTPS /v1 URL, or HTTP to loopback or a private host"
        )
    return url.rstrip("/")


def read_key_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigurationError(
            f"Cannot read the embeddings key file {path}: {error.strerror}"
        ) from None
    if not value:
        raise ConfigurationError(f"The embeddings key file {path} is empty")
    return value


class OpenAICompatibleEmbeddings:
    """Batches by count, retries throttling and transient failures (bounded), validates all."""

    def __init__(
        self,
        session: Any,
        *,
        base_url: str,
        model: str,
        served_model: str,
        dimensions: int,
        api_key: str | None = None,
        max_inputs_per_request: int = 32,
        concurrency: int = 2,
        attempts: int = 4,
    ) -> None:
        self._session = session
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._served_model = served_model
        self._dimensions = dimensions
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._max_inputs = max_inputs_per_request
        self._semaphore = asyncio.Semaphore(concurrency)
        self._attempts = attempts

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        if any(not text.strip() for text in texts):
            raise InvalidInputError("Embedding inputs must be non-empty")
        batches = [
            list(range(start, min(start + self._max_inputs, len(texts))))
            for start in range(0, len(texts), self._max_inputs)
        ]
        results: list[Vector | None] = [None] * len(texts)

        async def run(batch: list[int]) -> None:
            async with self._semaphore:
                vectors = await self._request([texts[index] for index in batch])
            for index, vector in zip(batch, vectors, strict=True):
                results[index] = vector

        try:
            async with asyncio.TaskGroup() as group:
                for batch in batches:
                    group.create_task(run(batch))
        except ExceptionGroup as errors:
            # Surface one classified error; the TaskGroup already cancelled sibling batches.
            raise errors.exceptions[0] from errors
        return [vector for vector in results if vector is not None]

    async def _request(self, inputs: list[str]) -> list[Vector]:
        body = {"model": self._served_model, "input": inputs}
        for attempt in range(self._attempts):
            outcome = await self._attempt(body)
            if isinstance(outcome, list):
                return reconcile_embeddings(len(inputs), outcome, dimensions=self._dimensions)
            if not isinstance(outcome, ThrottledError | TransientError):
                raise outcome
            if attempt == self._attempts - 1:
                raise outcome
            await asyncio.sleep(min(outcome.retry_after or 2.0**attempt, 30.0))
        raise TransientError("No embeddings attempt was made")

    async def _attempt(self, body: dict[str, Any]) -> list[tuple[int, Any]] | PipelineError:
        import aiohttp

        try:
            async with self._session.post(self._url, json=body, headers=self._headers) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    return for_status(
                        response.status,
                        f"Embeddings request failed with HTTP {response.status}: {detail}",
                        retry_after=parse_retry_after(dict(response.headers)),
                    )
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            return TransientError(f"Embeddings transport failure: {type(error).__name__}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not all(
            isinstance(item, dict) and isinstance(item.get("index"), int) for item in data
        ):
            return IntegrityError("Embeddings response has an unexpected shape")
        return [(item["index"], item.get("embedding") or []) for item in data]
