from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from fastapi import FastAPI
from langchain_openai import ChatOpenAI

from crv_agent.adapters.knowledge import PostgresKnowledge
from crv_agent.adapters.models import LangChainModels, reject_unapproved_tracing
from crv_agent.adapters.postgres.memory import checkpointer, entra_pool
from crv_agent.adapters.postgres.persistence import PostgresPersistence
from crv_agent.adapters.search import AzureSearch
from crv_agent.api.app import create_app
from crv_agent.api.dependencies import AppServices
from crv_agent.api.identity import EntraIdentity, Identity, LocalIdentity
from crv_agent.orchestration.graph import build_graph
from crv_agent.orchestration.workers import EvidenceWorker
from crv_agent.runtime.settings import Settings
from crv_agent.services.retrieval import Retrieval


def application(settings: Settings) -> FastAPI:
    reject_unapproved_tracing()
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            credential = await stack.enter_async_context(DefaultAzureCredential())
            search_client = await stack.enter_async_context(SearchClient(
                settings.search_endpoint, settings.search_index, credential,
                retry_total=0, connection_timeout=10, read_timeout=20,
            ))
            kg_pool = await stack.enter_async_context(entra_pool(
                credential, settings.postgres_host, settings.kg_database, settings.postgres_user,
                read_only=True,
                root_cert=settings.postgres_root_cert,
            ))
            memory_pool = await stack.enter_async_context(entra_pool(
                credential, settings.postgres_host, settings.memory_database, settings.memory_user,
                read_only=False,
                root_cert=settings.postgres_root_cert,
            ))
            token_provider = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default",
            )

            def model(deployment: str) -> ChatOpenAI:
                endpoint = settings.openai_endpoint.rstrip("/")
                if not endpoint.endswith("/openai/v1"):
                    endpoint += "/openai/v1"
                return ChatOpenAI(
                    model=deployment, base_url=endpoint + "/", api_key=token_provider,
                    max_completion_tokens=settings.output_tokens,
                    max_retries=0, timeout=30, include_response_headers=False,
                )

            planner = model(settings.planner_deployment)
            worker_model = model(settings.worker_deployment)
            answer_model = model(settings.answer_deployment)
            verifier = model(settings.verifier_deployment)
            for chat_model in (planner, worker_model, answer_model, verifier):
                stack.push_async_callback(chat_model.root_async_client.close)
                stack.callback(chat_model.root_client.close)
            search = AzureSearch(search_client, settings.search_release)
            knowledge = PostgresKnowledge(kg_pool, settings.kg_scopes)
            persistence = PostgresPersistence(memory_pool)

            async def ready() -> None:
                await search.ready()
                await knowledge.ready(settings.kg_release)
                async with memory_pool.connection() as connection:
                    await connection.execute(
                        "SELECT id FROM crv_threads LIMIT 0",
                    )
                    await connection.execute("SELECT thread_id FROM checkpoints LIMIT 0")

            await ready()
            identity: Identity
            if settings.local_owner:
                identity = LocalIdentity(settings.local_owner)
            else:
                identity = EntraIdentity(settings.tenant_id or "", settings.audience or "")
            app.state.services = AppServices(
                persistence=persistence, identity=identity,
                models=LangChainModels(planner, answer_model, verifier, settings.output_tokens),
                worker=EvidenceWorker(worker_model, Retrieval(search), settings.output_tokens),
                knowledge=knowledge, graph=build_graph(checkpointer(memory_pool)),
                search_release=settings.search_release, kg_release=settings.kg_release,
                ready=ready, model_calls=settings.model_calls, turn_tokens=settings.turn_tokens,
                retrieval_mode=settings.retrieval_mode,
            )
            yield
    return create_app(lifespan=lifespan)
