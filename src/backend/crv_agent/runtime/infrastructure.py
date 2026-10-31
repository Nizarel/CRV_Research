import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import ContainerClient
from langchain_openai import ChatOpenAI
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from crv_agent.adapters.blob import BlobSources
from crv_agent.adapters.knowledge import PostgresKnowledge
from crv_agent.adapters.local_sources import FilesystemSources
from crv_agent.adapters.models import reject_unapproved_tracing
from crv_agent.adapters.postgres.memory import pg_pool
from crv_agent.adapters.postgres.search import CrossEncoder, PostgresSearch, QueryEmbedder
from crv_agent.adapters.retrieval_models import (
    AzureOpenAIQueryEmbedder,
    CohereReranker,
    OpenAICompatibleQueryEmbedder,
    TeiReranker,
    Token,
    static_token,
)
from crv_agent.adapters.search import AzureSearch
from crv_agent.adapters.secrets import read_secret_file
from crv_agent.ports.retrieval import Search
from crv_agent.ports.sources import Sources
from crv_agent.runtime.database import kg_profile, memory_profile
from crv_agent.runtime.settings import Settings

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
logger = logging.getLogger(__name__)
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
VECTOR_DIMENSIONS = {"te3": 3072, "m3": 1024}
# vLLM and similar servers ignore the key unless started with one; the SDK needs a value.
UNAUTHENTICATED_KEY = "unused"


@dataclass(frozen=True)
class Infrastructure:
    search: Search
    knowledge: PostgresKnowledge
    kg_pool: Pool
    memory_pool: Pool | None
    models: dict[str, ChatOpenAI]
    sources: Sources | None = None


def needs_azure_credential(settings: Settings, *, memory: bool = True) -> bool:
    """True when any configured component authenticates with Entra."""
    postgres = settings.search_backend == "postgres"
    return any((
        kg_profile(settings).auth == "entra",
        memory and memory_profile(settings).auth == "entra",
        settings.search_backend == "azure",
        settings.effective_model_auth == "entra",
        postgres and settings.pg_embedder == "azure",
        postgres and settings.pg_reranker == "cohere",
        bool(settings.sources_account_url),
    ))


def _key_token(path: str | None, what: str) -> Token | None:
    return static_token(read_secret_file(path, what)) if path else None


def close_clients(stack: AsyncExitStack, chat_model: ChatOpenAI) -> None:
    # An async Entra token provider yields only an async client (root_client is None).
    if chat_model.root_async_client is not None:
        stack.push_async_callback(chat_model.root_async_client.close)
    if chat_model.root_client is not None:
        stack.callback(chat_model.root_client.close)


class NotFoundRetry(httpx.AsyncBaseTransport):
    """Resends a request answered with HTTP 404, a bounded number of times with backoff."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, retries: int, delay: float = 1.0,
    ) -> None:
        self.inner, self.retries, self.delay = inner, retries, delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self.retries + 1):
            response = await self.inner.handle_async_request(request)
            if response.status_code != 404 or attempt == self.retries:
                return response
            await response.aclose()
            logger.warning("model_not_found_retry attempt=%d", attempt + 1)
            await asyncio.sleep(self.delay * 2 ** attempt)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.inner.aclose()


def create_chat_models(
    stack: AsyncExitStack, settings: Settings,
    token_provider: Callable[[], Awaitable[str]] | None,
) -> dict[str, ChatOpenAI]:
    endpoint = (settings.model_endpoint or settings.openai_endpoint or "").rstrip("/")
    # Azure resources serve /openai/v1; other OpenAI-compatible servers are given as their /v1.
    if not endpoint.endswith("/v1"):
        endpoint += "/openai/v1"
    auth = settings.effective_model_auth
    api_key: SecretStr | str | Callable[[], Awaitable[str]]
    if auth == "key":
        api_key = settings.model_api_key or SecretStr(
            read_secret_file(settings.model_api_key_file or "", "model API key"),
        )
    elif auth == "none":
        api_key = UNAUTHENTICATED_KEY
    else:
        if token_provider is None:
            raise ValueError("Entra model access needs an Azure credential")
        api_key = token_provider
    models: dict[str, ChatOpenAI] = {}
    http_async_client = None
    if settings.model_not_found_retries:
        http_async_client = httpx.AsyncClient(transport=NotFoundRetry(
            httpx.AsyncHTTPTransport(), settings.model_not_found_retries,
        ))
        stack.push_async_callback(http_async_client.aclose)
    for role, deployment, effort in (
        ("planner", settings.planner_deployment, settings.planner_reasoning_effort),
        ("worker", settings.worker_deployment, settings.worker_reasoning_effort),
        ("answer", settings.answer_deployment, settings.answer_reasoning_effort),
        ("verifier", settings.verifier_deployment, settings.verifier_reasoning_effort),
    ):
        chat_model = ChatOpenAI(
            model=deployment, base_url=endpoint + "/", api_key=api_key,
            max_completion_tokens=(
                settings.worker_output_tokens if role == "worker" else settings.output_tokens
            ),
            reasoning_effort=effort,
            max_retries=settings.model_retries, timeout=60, include_response_headers=False,
            http_async_client=http_async_client,
        )
        close_clients(stack, chat_model)
        models[role] = chat_model
    return models


@asynccontextmanager
async def infrastructure(
    settings: Settings, *, memory: bool = True,
) -> AsyncIterator[Infrastructure]:
    """Create lifecycle-managed clients shared by the API, checks and evaluation. The Azure
    credential exists only when a configured component authenticates with Entra."""
    reject_unapproved_tracing()
    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(
            DefaultAzureCredential(),
        ) if needs_azure_credential(settings, memory=memory) else None
        token_provider = (
            get_bearer_token_provider(credential, COGNITIVE_SCOPE) if credential else None
        )
        kg_pool = await stack.enter_async_context(
            pg_pool(kg_profile(settings), credential, read_only=True),
        )
        search = await _search(stack, settings, credential, token_provider)
        memory_pool = await stack.enter_async_context(
            pg_pool(memory_profile(settings), credential, read_only=False),
        ) if memory else None
        models = create_chat_models(stack, settings, token_provider)
        sources: Sources | None = None
        if settings.sources_account_url:
            sources = BlobSources(await stack.enter_async_context(ContainerClient(
                settings.sources_account_url, settings.sources_container, credential,
            )))
        elif settings.sources_root:
            sources = FilesystemSources(Path(settings.sources_root))
        yield Infrastructure(
            search=search,
            knowledge=PostgresKnowledge(kg_pool, settings.kg_scopes),
            kg_pool=kg_pool, memory_pool=memory_pool, models=models, sources=sources,
        )


def _required(token_provider: Token | None) -> Token:
    if token_provider is None:
        raise ValueError("This Azure component needs an Azure credential")
    return token_provider


async def _search(
    stack: AsyncExitStack, settings: Settings, credential: DefaultAzureCredential | None,
    token_provider: Token | None,
) -> Search:
    if settings.search_backend == "azure":
        assert settings.search_endpoint is not None and credential is not None
        client = await stack.enter_async_context(SearchClient(
            settings.search_endpoint, settings.search_index, credential,
            retry_total=0, connection_timeout=10, read_timeout=20,
        ))
        return AzureSearch(client, settings.search_release)
    pool = await stack.enter_async_context(pg_pool(
        kg_profile(settings), credential, read_only=True, max_size=settings.pg_search_pool_size,
    ))
    session = await stack.enter_async_context(
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)),
    )
    dimensions = VECTOR_DIMENSIONS[settings.pg_vector_model]
    embedder: QueryEmbedder
    if settings.pg_embedder == "azure":
        embedder = AzureOpenAIQueryEmbedder(
            session, settings.openai_endpoint or "", settings.pg_embedding_deployment,
            _required(token_provider), dimensions=dimensions,
        )
    else:
        embedder = OpenAICompatibleQueryEmbedder(
            session, settings.pg_embedding_url, settings.pg_embedding_model,
            _key_token(settings.pg_embedding_api_key_file, "embedding API key"),
            dimensions=dimensions,
        )
    reranker: CrossEncoder | None = None
    rerank_key = _key_token(settings.pg_reranker_api_key_file, "reranker API key")
    if settings.pg_reranker == "cohere":
        reranker = CohereReranker(
            session, settings.pg_reranker_url, settings.pg_reranker_model,
            _required(token_provider),
        )
    elif settings.pg_reranker == "vllm":
        reranker = CohereReranker(
            session, settings.pg_reranker_url, settings.pg_reranker_model, rerank_key,
        )
    elif settings.pg_reranker == "tei":
        reranker = TeiReranker(session, settings.pg_reranker_url, rerank_key)
    return PostgresSearch(
        pool, settings.search_release, embedder, model=settings.pg_vector_model,
        reranker=reranker,
    )
