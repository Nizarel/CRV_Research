from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from crv_agent.adapters.models import (
    LangChainModels,
    LangChainReranker,
    reject_unapproved_tracing,
)
from crv_agent.adapters.postgres.memory import checkpointer
from crv_agent.adapters.postgres.persistence import PostgresPersistence
from crv_agent.api.app import create_app
from crv_agent.api.dependencies import AppServices
from crv_agent.api.identity import EntraIdentity, Identity, LocalIdentity
from crv_agent.orchestration.extraction import ExtractionWorker
from crv_agent.orchestration.graph import build_graph
from crv_agent.orchestration.workers import EvidenceWorker, Worker
from crv_agent.runtime.infrastructure import infrastructure
from crv_agent.runtime.operations import memory_ready
from crv_agent.runtime.settings import Settings, evidence_policy
from crv_agent.services.retrieval import Retrieval


def application(settings: Settings) -> FastAPI:
    reject_unapproved_tracing()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with infrastructure(settings) as infra:
            memory_pool = infra.memory_pool
            assert memory_pool is not None

            async def ready() -> None:
                await infra.search.ready()
                await infra.knowledge.ready(settings.kg_release)
                await memory_ready(memory_pool)

            await ready()
            identity: Identity
            if settings.local_owner:
                identity = LocalIdentity(settings.local_owner)
            else:
                identity = EntraIdentity(settings.tenant_id or "", settings.audience or "")
            models = infra.models
            language_models = LangChainModels(
                models["planner"], models["answer"], models["verifier"], settings.output_tokens,
            )
            policy = evidence_policy(settings)
            reranker = (
                LangChainReranker(language_models, models["worker"])
                if policy.rerank == "llm" else None
            )
            retrieval = Retrieval(infra.search)
            worker: Worker = ExtractionWorker(
                models["worker"], retrieval, settings.worker_output_tokens, policy, reranker,
                settings.worker_max_claims, settings.extract_batches,
            ) if settings.worker_mode == "extract" else EvidenceWorker(
                models["worker"], retrieval, settings.worker_output_tokens, policy, reranker,
            )
            app.state.services = AppServices(
                persistence=PostgresPersistence(memory_pool), identity=identity,
                models=language_models, worker=worker,
                knowledge=infra.knowledge, graph=build_graph(checkpointer(memory_pool)),
                search_release=settings.search_release, kg_release=settings.kg_release,
                ready=ready, model_calls=settings.model_calls, turn_tokens=settings.turn_tokens,
                turn_seconds=settings.turn_seconds, facet_concurrency=settings.facet_concurrency,
                retrieval_mode=settings.retrieval_mode, diagnostics=settings.eval_diagnostics,
                repair_mode=settings.repair_mode, kg_results=settings.kg_results,
            )
            yield
    return create_app(lifespan=lifespan)
