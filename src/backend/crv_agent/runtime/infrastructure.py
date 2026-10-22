from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from langchain_openai import ChatOpenAI
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.knowledge import PostgresKnowledge
from crv_agent.adapters.models import reject_unapproved_tracing
from crv_agent.adapters.postgres.memory import entra_pool
from crv_agent.adapters.search import AzureSearch
from crv_agent.runtime.settings import Settings

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]


@dataclass(frozen=True)
class Infrastructure:
    search: AzureSearch
    knowledge: PostgresKnowledge
    kg_pool: Pool
    memory_pool: Pool | None
    models: dict[str, ChatOpenAI]


def close_clients(stack: AsyncExitStack, chat_model: ChatOpenAI) -> None:
    # An async Entra token provider yields only an async client (root_client is None).
    if chat_model.root_async_client is not None:
        stack.push_async_callback(chat_model.root_async_client.close)
    if chat_model.root_client is not None:
        stack.callback(chat_model.root_client.close)


@asynccontextmanager
async def infrastructure(
    settings: Settings, *, memory: bool = True,
) -> AsyncIterator[Infrastructure]:
    """Create lifecycle-managed Azure clients shared by the API, checks and evaluation."""
    reject_unapproved_tracing()
    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(DefaultAzureCredential())
        search_client = await stack.enter_async_context(SearchClient(
            settings.search_endpoint, settings.search_index, credential,
            retry_total=0, connection_timeout=10, read_timeout=20,
        ))
        kg_pool = await stack.enter_async_context(entra_pool(
            credential, settings.postgres_host, settings.kg_database, settings.postgres_user,
            read_only=True, root_cert=settings.postgres_root_cert,
        ))
        memory_pool = await stack.enter_async_context(entra_pool(
            credential, settings.postgres_host, settings.memory_database, settings.memory_user,
            read_only=False, root_cert=settings.postgres_root_cert,
        )) if memory else None
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default",
        )
        endpoint = settings.openai_endpoint.rstrip("/")
        if not endpoint.endswith("/openai/v1"):
            endpoint += "/openai/v1"
        models: dict[str, ChatOpenAI] = {}
        for role, deployment in (
            ("planner", settings.planner_deployment), ("worker", settings.worker_deployment),
            ("answer", settings.answer_deployment), ("verifier", settings.verifier_deployment),
        ):
            chat_model = ChatOpenAI(
                model=deployment, base_url=endpoint + "/", api_key=token_provider,
                max_completion_tokens=(
                    settings.worker_output_tokens if role == "worker" else settings.output_tokens
                ),
                max_retries=settings.model_retries, timeout=60, include_response_headers=False,
            )
            close_clients(stack, chat_model)
            models[role] = chat_model
        yield Infrastructure(
            search=AzureSearch(search_client, settings.search_release),
            knowledge=PostgresKnowledge(kg_pool, settings.kg_scopes),
            kg_pool=kg_pool, memory_pool=memory_pool, models=models,
        )
