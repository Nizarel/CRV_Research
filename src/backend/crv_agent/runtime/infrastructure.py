from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import aiohttp
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import ContainerClient
from langchain_openai import ChatOpenAI
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.blob import BlobSources
from crv_agent.adapters.knowledge import PostgresKnowledge
from crv_agent.adapters.models import reject_unapproved_tracing
from crv_agent.adapters.postgres.memory import entra_pool
from crv_agent.adapters.postgres.search import PostgresSearch
from crv_agent.adapters.retrieval_models import AzureOpenAIQueryEmbedder, CohereReranker
from crv_agent.adapters.search import AzureSearch
from crv_agent.ports.retrieval import Search
from crv_agent.runtime.settings import Settings

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class Infrastructure:
    search: Search
    knowledge: PostgresKnowledge
    kg_pool: Pool
    memory_pool: Pool | None
    models: dict[str, ChatOpenAI]
    sources: BlobSources | None = None


def close_clients(stack: AsyncExitStack, chat_model: ChatOpenAI) -> None:
    # An async Entra token provider yields only an async client (root_client is None).
    if chat_model.root_async_client is not None:
        stack.push_async_callback(chat_model.root_async_client.close)
    if chat_model.root_client is not None:
        stack.callback(chat_model.root_client.close)


def create_chat_models(
    stack: AsyncExitStack, settings: Settings,
    token_provider: Callable[[], Awaitable[str]],
) -> dict[str, ChatOpenAI]:
    endpoint = (settings.model_endpoint or settings.openai_endpoint).rstrip("/")
    if not endpoint.endswith("/openai/v1"):
        endpoint += "/openai/v1"
    api_key = (
        settings.model_api_key.get_secret_value() if settings.model_api_key else token_provider
    )
    models: dict[str, ChatOpenAI] = {}
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
        )
        close_clients(stack, chat_model)
        models[role] = chat_model
    return models


@asynccontextmanager
async def infrastructure(
    settings: Settings, *, memory: bool = True,
) -> AsyncIterator[Infrastructure]:
    """Create lifecycle-managed Azure clients shared by the API, checks and evaluation."""
    reject_unapproved_tracing()
    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(DefaultAzureCredential())
        token_provider = get_bearer_token_provider(credential, COGNITIVE_SCOPE)
        kg_pool = await stack.enter_async_context(entra_pool(
            credential, settings.postgres_host, settings.kg_database, settings.postgres_user,
            read_only=True, root_cert=settings.postgres_root_cert,
        ))
        search = await _search(stack, settings, credential, token_provider)
        memory_pool = await stack.enter_async_context(entra_pool(
            credential, settings.postgres_host, settings.memory_database, settings.memory_user,
            read_only=False, root_cert=settings.postgres_root_cert,
        )) if memory else None
        models = create_chat_models(stack, settings, token_provider)
        sources = BlobSources(await stack.enter_async_context(ContainerClient(
            settings.sources_account_url, settings.sources_container, credential,
        ))) if settings.sources_account_url else None
        yield Infrastructure(
            search=search,
            knowledge=PostgresKnowledge(kg_pool, settings.kg_scopes),
            kg_pool=kg_pool, memory_pool=memory_pool, models=models, sources=sources,
        )


async def _search(
    stack: AsyncExitStack, settings: Settings, credential: DefaultAzureCredential,
    token_provider: Callable[[], Awaitable[str]],
) -> Search:
    if settings.search_backend == "azure":
        client = await stack.enter_async_context(SearchClient(
            settings.search_endpoint, settings.search_index, credential,
            retry_total=0, connection_timeout=10, read_timeout=20,
        ))
        return AzureSearch(client, settings.search_release)
    pool = await stack.enter_async_context(entra_pool(
        credential, settings.postgres_host, settings.kg_database, settings.postgres_user,
        read_only=True, root_cert=settings.postgres_root_cert,
        max_size=settings.pg_search_pool_size,
    ))
    session = await stack.enter_async_context(
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)),
    )
    reranker = CohereReranker(
        session, settings.pg_reranker_url, settings.pg_reranker_model, token_provider,
    ) if settings.pg_reranker == "cohere" else None
    return PostgresSearch(
        pool, settings.search_release,
        AzureOpenAIQueryEmbedder(
            session, settings.openai_endpoint, settings.pg_embedding_deployment, token_provider,
        ),
        reranker=reranker,
    )
