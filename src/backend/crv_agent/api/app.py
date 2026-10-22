import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from starlette.requests import Request

from crv_agent.api.dependencies import AppServices
from crv_agent.api.events import diagnostics, event
from crv_agent.api.identity import AuthenticationError
from crv_agent.api.schemas import ChatRequest, FeedbackRequest
from crv_agent.domain.answers import Answer
from crv_agent.domain.execution import Budget
from crv_agent.orchestration.context import ResearchContext
from crv_agent.ports.persistence import Run, Thread, ThreadNotFound, TurnConflict

logger = logging.getLogger(__name__)


def leaf_types(error: BaseException) -> str:
    """Innermost exception class names (TaskGroup wraps failures in ExceptionGroups)."""
    if isinstance(error, BaseExceptionGroup):
        return ",".join(sorted({leaf_types(inner) for inner in error.exceptions}))
    return type(error).__name__


def create_app(
    services: AppServices | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="CRV research backend", version="0.1.0", lifespan=lifespan)
    if services:
        app.state.services = services

    def dependencies() -> AppServices:
        value: AppServices = app.state.services
        return value

    async def owner(
        deps: Annotated[AppServices, Depends(dependencies)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        try:
            return await deps.identity.owner(authorization)
        except AuthenticationError as error:
            raise HTTPException(401, "Authentication required") from error

    @app.exception_handler(ThreadNotFound)
    async def not_found(request: Request, error: ThreadNotFound) -> JSONResponse:
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(TurnConflict)
    async def conflict(request: Request, error: TurnConflict) -> JSONResponse:
        return JSONResponse({"error": "turn_conflict"}, status_code=409)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "mode": "development_only"}

    @app.get("/health/ready")
    async def ready(deps: Annotated[AppServices, Depends(dependencies)]) -> dict[str, str]:
        await deps.ready()
        return {"status": "ready", "authorization": "deferred"}

    @app.post("/api/v1/conversations")
    async def create(
        principal: Annotated[str, Depends(owner)],
        deps: Annotated[AppServices, Depends(dependencies)],
    ) -> Thread:
        return await deps.persistence.create_thread(principal, deps.search_release, deps.kg_release)

    @app.get("/api/v1/conversations")
    async def threads(
        principal: Annotated[str, Depends(owner)],
        deps: Annotated[AppServices, Depends(dependencies)],
    ) -> list[Thread]:
        return await deps.persistence.threads(principal)

    @app.get("/api/v1/conversations/{thread_id}")
    async def history(
        thread_id: UUID, principal: Annotated[str, Depends(owner)],
        deps: Annotated[AppServices, Depends(dependencies)],
    ) -> list[Run]:
        return await deps.persistence.history(principal, str(thread_id))

    @app.post("/api/v1/feedback", status_code=204)
    async def feedback(
        body: FeedbackRequest, principal: Annotated[str, Depends(owner)],
        deps: Annotated[AppServices, Depends(dependencies)],
    ) -> None:
        await deps.persistence.feedback(
            principal, str(body.thread_id), str(body.request_id), body.rating, body.comment,
        )

    @app.post("/api/v1/chat")
    async def chat(
        body: ChatRequest, request: Request, principal: Annotated[str, Depends(owner)],
        deps: Annotated[AppServices, Depends(dependencies)],
    ) -> StreamingResponse:
        thread_id, run_id = str(body.thread_id), str(body.request_id)
        thread = await deps.persistence.thread(principal, thread_id)
        if (thread.search_release, thread.kg_release) != (deps.search_release, deps.kg_release):
            raise HTTPException(409, "Thread release pins unavailable; create a new conversation")
        history = await deps.persistence.history(principal, thread_id)
        run, created = await deps.persistence.start_run(principal, thread_id, run_id, body.message)

        async def stream() -> AsyncIterator[str]:
            sequence = 0
            if not created:
                yield event("done", {
                    "status": run.status, "result": run.result, "replayed": True,
                }, sequence, run_id)
                return
            context = ResearchContext(
                models=deps.models, knowledge=deps.knowledge, worker=deps.worker,
                budget=Budget(
                    model_limit=deps.model_calls, token_limit=deps.turn_tokens,
                    seconds=deps.turn_seconds,
                ),
                search_release=thread.search_release, kg_release=thread.kg_release,
                trace_id=str(uuid4()), retrieval_mode=deps.retrieval_mode,
                facet_concurrency=deps.facet_concurrency,
            )
            final: Answer | None = None
            persisted = False
            try:
                yield event("progress", {"step": "started"}, sequence, run_id)
                sequence += 1
                with trace.get_tracer("crv_agent").start_as_current_span("research_turn") as span:
                    span.set_attribute("crv.run_id", run_id)
                    span.set_attribute("crv.trace_id", context.trace_id)
                    span.set_attribute("crv.search_release", context.search_release)
                    span.set_attribute("crv.kg_release", context.kg_release)
                    async with asyncio.timeout(context.budget.seconds):
                        async for update in deps.graph.astream(
                            {"question": body.message, "history": [
                                f"User: {previous.question}\nAssistant: "
                                f"{(previous.result or {}).get('content', '')}"
                                for previous in history[-7:] if previous.status == "completed"
                            ]},
                            {"configurable": {"thread_id": f"{thread_id}:{run_id}"}},
                            context=context, stream_mode="updates",
                        ):
                            if await request.is_disconnected():
                                raise asyncio.CancelledError
                            for step, values in update.items():
                                if step == "answer":
                                    final = Answer.model_validate(values["answer"])
                                yield event("progress", {"step": step}, sequence, run_id)
                                sequence += 1
                                if step == "research" and deps.diagnostics:
                                    yield event(
                                        "diagnostics", diagnostics(values or {}), sequence, run_id,
                                    )
                                    sequence += 1
                if final is None:
                    raise RuntimeError("Research graph produced no final answer")
                payload = final.model_dump(mode="json")
                await deps.persistence.finish_run(
                    principal, thread_id, run_id, "completed", payload,
                )
                persisted = True
                yield event("answer", payload, sequence, run_id)
                yield event("done", {"status": "completed"}, sequence + 1, run_id)
            except asyncio.CancelledError:
                if not persisted:
                    await asyncio.shield(deps.persistence.finish_run(
                        principal, thread_id, run_id, "cancelled", {"error": "cancelled"},
                    ))
                raise
            except Exception as error:
                logger.error(
                    "research_turn_failed run_id=%s error_type=%s",
                    run_id, leaf_types(error),
                )
                try:
                    await deps.persistence.finish_run(
                        principal, thread_id, run_id, "failed", {"error": type(error).__name__},
                    )
                except Exception as persistence_error:
                    logger.error(
                        "turn_failure_persistence_failed run_id=%s error_type=%s",
                        run_id, type(persistence_error).__name__,
                    )
                yield event("error", {
                    "error": "research_failed", "error_type": leaf_types(error),
                    "trace_id": context.trace_id,
                }, sequence, run_id)
        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
