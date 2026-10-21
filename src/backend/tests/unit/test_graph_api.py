import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from crv_agent.adapters.postgres.memory import serializer
from crv_agent.adapters.search import query_filter
from crv_agent.api.app import create_app
from crv_agent.api.dependencies import AppServices
from crv_agent.api.identity import AuthenticationError, LocalIdentity
from crv_agent.domain.answers import Answer
from crv_agent.domain.execution import Budget
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.graph import build_graph
from tests.fakes import (
    CLAIM,
    SOURCE,
    FakeKnowledge,
    FakeModels,
    FakeWorker,
    MemoryPersistence,
)


def services(models: FakeModels | None = None) -> AppServices:
    async def ready() -> None:
        return None

    return AppServices(
        persistence=MemoryPersistence(), identity=LocalIdentity("owner"),
        models=models or FakeModels(), worker=FakeWorker(), knowledge=FakeKnowledge(),
        graph=build_graph(InMemorySaver(serde=serializer())),
        search_release="r1", kg_release="kg1", ready=ready,
    )


def test_filter_escapes_release_and_rejects_hash_injection() -> None:
    assert query_filter("r'1", []) == "release_id eq 'r''1'"
    with pytest.raises(ValueError):
        query_filter("r1", ["') or true"])


def test_strict_serializer_roundtrip_and_rejects_unapproved_class() -> None:
    serde = serializer()
    assert serde.loads_typed(serde.dumps_typed(CLAIM)) == CLAIM
    assert serde.loads_typed(serde.dumps_typed(SOURCE)) == SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["lookup", "synthesis", "timeline", "comparison"])
async def test_research_routes_checkpoint_and_verify(intent: str) -> None:
    deps = services(FakeModels(intent=intent))
    context = ResearchContext(
        models=deps.models, knowledge=deps.knowledge, worker=deps.worker, budget=Budget(),
        search_release="r1", kg_release="kg1", trace_id="test",
    )
    result = await deps.graph.ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}}, context=context,
    )
    answer = Answer.model_validate(result["answer"])
    assert answer.status in {"answered", "partial"}
    assert answer.citations == [SOURCE]


@pytest.mark.asyncio
async def test_verification_repairs_once_then_abstains() -> None:
    models = FakeModels(supported=False)
    deps = services(models)
    result = await deps.graph.ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=deps.knowledge, worker=deps.worker, budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="test",
        ),
    )
    assert result["answer"].status == "insufficient_evidence"
    assert not result["answer"].citations
    assert models.compositions == 2


def test_api_persists_before_answer_and_replays_without_models() -> None:
    models = FakeModels()
    deps = services(models)
    with TestClient(create_app(deps)) as client:
        thread = client.post("/api/v1/conversations").json()
        request = {
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?",
        }
        response = client.post("/api/v1/chat", json=request)
        assert response.status_code == 200
        events = [
            block.splitlines()[1] for block in response.text.strip().split("\n\n")
        ]
        assert events[-2:] == ["event: answer", "event: done"]
        history = client.get(f"/api/v1/conversations/{thread['id']}").json()
        assert history[0]["status"] == "completed"
        assert history[0]["result"]["citations"][0]["pages"] == [4]
        before = models.compositions
        replay = client.post("/api/v1/chat", json=request)
        assert '"replayed": true' in replay.text
        assert models.compositions == before
        request["message"] = "Different question"
        assert client.post("/api/v1/chat", json=request).status_code == 409


def test_api_owner_isolation_and_authentication() -> None:
    deps = services()
    app = create_app(deps)
    with TestClient(app) as client:
        thread = client.post("/api/v1/conversations").json()
        object.__setattr__(deps, "identity", LocalIdentity("other"))
        assert client.get(f"/api/v1/conversations/{thread['id']}").status_code == 404
        assert client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?",
        }).status_code == 404

        class Rejected:
            async def owner(self, authorization: str | None) -> str:
                raise AuthenticationError("invalid")
        object.__setattr__(deps, "identity", Rejected())
        assert client.get("/api/v1/conversations").status_code == 401


def test_clarification_emits_no_unsupported_answer() -> None:
    deps = services(FakeModels("clarify"))
    with TestClient(create_app(deps)) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Which one?",
        })
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines() if line.startswith("data: ")
        ]
        answer = next(payload for payload in payloads if "content" in payload)
        assert answer["status"] == "needs_clarification"
        assert answer["citations"] == []


def test_provider_failure_is_not_an_absence_answer() -> None:
    class Failed(FakeModels):
        async def plan(self, question: str, history: list[str], budget: Budget):
            raise ConnectionError("Provider down")

    deps = services(Failed())
    with TestClient(create_app(deps)) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?",
        })
        assert "event: error" in response.text
        assert "event: answer" not in response.text
        assert client.get(f"/api/v1/conversations/{thread['id']}").json()[0]["status"] == "failed"


def test_body_cannot_set_principal_and_blank_input_is_rejected() -> None:
    with TestClient(create_app(services())) as client:
        thread = client.post("/api/v1/conversations").json()
        body = {"thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?"}
        assert client.post("/api/v1/chat", json={**body, "owner": "someone"}).status_code == 422
        assert client.post("/api/v1/chat", json={**body, "message": "   "}).status_code == 422
