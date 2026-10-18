from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import openai
import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
)

from ingest_pipeline.adapters.azure_ai.embeddings import (
    AzureOpenAIEmbeddings,
    translate_openai_error,
    utf8_token_upper_bound,
)
from ingest_pipeline.adapters.azure_errors import parse_retry_after, translate_azure_error
from ingest_pipeline.adapters.parsers.tokenizers import ApproximateCounter, TiktokenCounter
from ingest_pipeline.domain.errors import (
    AuthError,
    IntegrityError,
    InvalidInputError,
    ThrottledError,
    TransientError,
)
from ingest_pipeline.runtime.bootstrap import embedding_token_counter


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "7"}, 7.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"x-ms-retry-after-ms": "250", "Retry-After": "9"}, 0.25),
        ({"Retry-After": "Thu, 01 Jan 2026 00:00:30 GMT"}, 30.0),
        ({"Retry-After": "soon"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_parse_retry_after(headers, expected) -> None:
    assert parse_retry_after(headers, now=datetime(2026, 1, 1, tzinfo=UTC)) == expected


def _http_error(status: int, headers: dict[str, str] | None = None) -> HttpResponseError:
    error = HttpResponseError(message=f"HTTP {status}")
    error.status_code = status
    error.response = SimpleNamespace(headers=headers or {})
    return error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(429, {"Retry-After": "3"}), ThrottledError),
        (_http_error(403), AuthError),
        (_http_error(503), TransientError),
        (_http_error(400), InvalidInputError),
        (ClientAuthenticationError("no token"), AuthError),
        (ServiceRequestError("dns"), TransientError),
    ],
)
def test_translate_azure_error(error: Exception, expected: type) -> None:
    translated = translate_azure_error(error)
    assert isinstance(translated, expected)


def test_translate_azure_error_keeps_retry_after_and_ignores_foreign_errors() -> None:
    translated = translate_azure_error(_http_error(429, {"Retry-After": "3"}))
    assert translated is not None
    assert translated.retry_after == 3.0
    assert translate_azure_error(KeyError("x")) is None


def test_translate_openai_error() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/embeddings")
    throttled = openai.RateLimitError(
        "slow down",
        response=httpx.Response(429, headers={"retry-after": "4"}, request=request),
        body=None,
    )
    translated = translate_openai_error(throttled)
    assert isinstance(translated, ThrottledError)
    assert translated.retry_after == 4.0
    assert isinstance(
        translate_openai_error(openai.APIConnectionError(request=request)), TransientError
    )


class _Embeddings:
    def __init__(self, dims: int, *, drop: int | None = None, reverse: bool = True) -> None:
        self.dims = dims
        self.drop = drop
        self.reverse = reverse
        self.calls: list[list[str]] = []

    async def create(self, *, model: str, input: list[str]):
        self.calls.append(list(input))
        data = [
            SimpleNamespace(index=index, embedding=[float(len(text))] * self.dims)
            for index, text in enumerate(input)
            if index != self.drop
        ]
        if self.reverse:
            data.reverse()
        return SimpleNamespace(data=data)


def _provider(client: _Embeddings, **overrides: int) -> AzureOpenAIEmbeddings:
    options = {
        "max_inputs_per_request": 2,
        "max_tokens_per_request": 100,
        "max_tokens_per_input": 40,
    }
    options.update(overrides)
    return AzureOpenAIEmbeddings(
        SimpleNamespace(embeddings=client),
        deployment="tizert-embedding-3-large",
        model="text-embedding-3-large",
        dimensions=4,
        **options,
    )


async def test_embeddings_pack_requests_and_preserve_order() -> None:
    client = _Embeddings(4)
    vectors = await _provider(client).embed(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert all(len(call) <= 2 for call in client.calls)
    assert sorted(text for call in client.calls for text in call) == [
        "a",
        "bb",
        "ccc",
        "dddd",
        "eeeee",
    ]


async def test_embeddings_reject_oversized_and_empty_inputs_before_calling() -> None:
    client = _Embeddings(4)
    with pytest.raises(InvalidInputError, match="exceed 40 tokens"):
        await _provider(client).embed(["x" * 41])
    with pytest.raises(InvalidInputError, match="non-empty"):
        await _provider(client).embed(["ok", "   "])
    assert client.calls == []


async def test_embeddings_never_return_partial_results() -> None:
    with pytest.raises(IntegrityError, match="Missing"):
        await _provider(_Embeddings(4, drop=1)).embed(["a", "b"])
    with pytest.raises(IntegrityError, match="dimensions"):
        await _provider(_Embeddings(3)).embed(["a"])


def test_utf8_bound_never_underestimates_tokens() -> None:
    assert utf8_token_upper_bound("é") == 2
    assert utf8_token_upper_bound("pump") == 4


async def test_embedding_limit_counts_with_the_chunker_tokenizer() -> None:
    # Rollout 2026-10-13: a dot-leader contents page was 815 tokens but 9,663 bytes.
    class _Encoding:
        name = "cl100k_base"

        def encode(self, text: str, disallowed_special: tuple[str, ...] = ()) -> list[str]:
            return text.split()

    leaders = "1. Terminology " + "." * 60 + " 60"
    client = _Embeddings(4)
    provider = AzureOpenAIEmbeddings(
        SimpleNamespace(embeddings=client),
        deployment="tizert-embedding-3-large",
        model="text-embedding-3-large",
        dimensions=4,
        max_inputs_per_request=2,
        max_tokens_per_request=100,
        max_tokens_per_input=40,
        token_counter=embedding_token_counter(TiktokenCounter(_Encoding())),
    )
    assert len(await provider.embed([leaders])) == 1
    assert embedding_token_counter(ApproximateCounter()) is utf8_token_upper_bound
