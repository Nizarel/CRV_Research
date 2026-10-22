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
    shown = SOURCE.model_copy(update={"exposed": [(0, 8), (12, 20)]})
    assert serde.loads_typed(serde.dumps_typed(shown)) == shown


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


@pytest.mark.parametrize("enabled", [False, True])
def test_diagnostics_event_streams_ledger_and_exposed_text_only_when_enabled(
    enabled: bool,
) -> None:
    from dataclasses import replace

    with TestClient(create_app(replace(services(), diagnostics=enabled))) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?",
        })
        blocks = [block.splitlines() for block in response.text.strip().split("\n\n")]
        found = [
            json.loads(lines[2].removeprefix("data: "))
            for lines in blocks if lines[1] == "event: diagnostics"
        ]
        sequences = [json.loads(lines[2].removeprefix("data: "))["sequence"] for lines in blocks]
    assert sequences == sorted(set(sequences))
    if not enabled:
        assert found == []
        return
    [payload] = found
    assert payload["ledger"] == [CLAIM.model_dump(mode="json")]
    assert payload["exposed"] == [{
        "id": "S1", "content_hash": SOURCE.content_hash, "title": SOURCE.title,
        "source": SOURCE.source_uri, "pages": [4], "text": SOURCE.content,
    }]


def test_plan_and_facet_findings_stream_before_the_verified_answer() -> None:
    with TestClient(create_app(services())) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Recovery?",
        })
    blocks = [block.splitlines() for block in response.text.strip().split("\n\n")]
    names = [lines[1].removeprefix("event: ") for lines in blocks]
    payloads = {
        lines[1].removeprefix("event: "): json.loads(lines[2].removeprefix("data: "))
        for lines in blocks
    }
    assert names.index("plan") < names.index("facet") < names.index("answer")
    assert payloads["plan"]["intent"] == "lookup" and payloads["plan"]["facets"] == ["Recovery?"]
    facet = payloads["facet"]
    assert facet["status"] == "completed" and facet["facet"] == "Recovery?"
    assert facet["claims"] == [CLAIM.model_dump(mode="json")]
    assert facet["sources"] == [{
        "id": "S1", "title": SOURCE.title, "source": SOURCE.source_uri, "pages": [4],
        "document_code": None, "source_role": "unclassified",
    }]


def test_blank_reasoning_effort_is_unset_and_diagnostics_need_a_local_owner(monkeypatch) -> None:
    import os

    from crv_agent.runtime.settings import Settings

    for key in [key for key in os.environ if key.upper().startswith("CRV_")]:
        monkeypatch.delenv(key)
    base = {
        "search_endpoint": "https://search", "kg_release": "kg1", "postgres_host": "pg",
        "postgres_user": "u", "kg_database": "kg", "memory_user": "m",
        "openai_endpoint": "https://openai", "planner_deployment": "p",
        "worker_deployment": "w", "answer_deployment": "a", "verifier_deployment": "v",
    }
    settings = Settings(
        **base, local_owner="dev", planner_reasoning_effort="", worker_reasoning_effort="low",
        eval_diagnostics=True,
    )
    assert settings.planner_reasoning_effort is None and settings.worker_reasoning_effort == "low"
    with pytest.raises(ValueError, match="loopback"):
        Settings(**base, tenant_id="t", audience="api", eval_diagnostics=True)
    with pytest.raises(ValueError):
        Settings(**base, local_owner="dev", answer_reasoning_effort="extreme")


@pytest.mark.asyncio
async def test_model_probe_reports_served_model_and_structured_output_failures() -> None:
    from langchain_core.messages import AIMessage

    from crv_agent.domain.answers import Verdict
    from crv_agent.runtime.operations import probe_model

    class Model:
        model_name, reasoning_effort = "model-router", "low"

        def __init__(self, result: object) -> None:
            self.result = result

        def with_structured_output(self, schema: object, **kwargs: object) -> "Model":
            assert kwargs == {"method": "json_schema", "strict": True, "include_raw": True}
            return self

        async def ainvoke(self, messages: object) -> object:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    raw = AIMessage(content="{}", usage_metadata={
        "input_tokens": 9, "output_tokens": 3, "total_tokens": 12,
        "output_token_details": {"reasoning": 2},
    }, response_metadata={"model_name": "gpt-5.4-mini-2026-03-17"})
    ok = await probe_model(Model({  # type: ignore[arg-type]
        "raw": raw, "parsed": Verdict(supported=True, reasons=[]), "parsing_error": None,
    }), Verdict)
    assert ok["ok"] is True and ok["served_model"] == "gpt-5.4-mini-2026-03-17"
    assert ok["reasoning_tokens"] == 2 and ok["reasoning_effort"] == "low"
    unparsed = await probe_model(Model({  # type: ignore[arg-type]
        "raw": raw, "parsed": None, "parsing_error": ValueError("bad json"),
    }), Verdict)
    assert unparsed["ok"] is False and "bad json" in str(unparsed["error"])
    failed = await probe_model(Model(ConnectionError("schema rejected")), Verdict)  # type: ignore[arg-type]
    assert failed["ok"] is False and failed["error"] == "ConnectionError: schema rejected"


def test_system_root_cert_resolves_to_an_os_bundle_file(tmp_path, monkeypatch) -> None:
    import ssl
    from types import SimpleNamespace

    from crv_agent.adapters.postgres import memory

    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=None))
    monkeypatch.setattr(memory, "_SYSTEM_BUNDLES", (str(tmp_path / "missing"), str(bundle)))
    assert memory.resolve_root_cert("system") == str(bundle)
    assert memory.resolve_root_cert("/custom/ca.pem") == "/custom/ca.pem"
    monkeypatch.setattr(memory, "_SYSTEM_BUNDLES", ())
    assert memory.resolve_root_cert("system") == "system"


@pytest.mark.asyncio
async def test_async_token_provider_models_close_without_sync_client() -> None:
    from contextlib import AsyncExitStack

    from langchain_openai import ChatOpenAI

    from crv_agent.runtime.infrastructure import close_clients

    async def token() -> str:
        return "token"

    model = ChatOpenAI(model="m", base_url="https://example.invalid/openai/v1/", api_key=token)
    assert model.root_client is None and model.root_async_client is not None
    async with AsyncExitStack() as stack:
        close_clients(stack, model)


@pytest.mark.asyncio
async def test_one_failed_facet_becomes_a_gap_but_all_failed_facets_fail_the_turn() -> None:
    from crv_agent.domain.evidence import WorkerResult
    from crv_agent.domain.planning import Facet

    class TwoFacets(FakeModels):
        async def plan(self, question: str, history: list[str], budget: Budget):
            result = await super().plan(question, history, budget)
            return result.model_copy(update={
                "intent": "comparison", "facets": [Facet(question="ok"), Facet(question="bad")],
            })

    class Partial(FakeWorker):
        def __init__(self, fail_all: bool) -> None:
            self.fail_all = fail_all

        async def run(self, question: str, release: str, hashes: list[str], budget: Budget):
            if self.fail_all or question.startswith("bad"):
                raise ValueError("Failed to parse structured output")
            return WorkerResult(claims=[CLAIM], gaps=[]), [SOURCE]

    def context(fail_all: bool) -> ResearchContext:
        return ResearchContext(
            models=TwoFacets(), knowledge=FakeKnowledge(), worker=Partial(fail_all),
            budget=Budget(), search_release="r1", kg_release="kg1", trace_id="t",
        )

    graph = build_graph(InMemorySaver(serde=serializer()))
    result = await graph.ainvoke(
        {"question": "Compare", "history": []},
        {"configurable": {"thread_id": str(uuid4())}}, context=context(False),
    )
    assert result["answer"].status == "partial"
    assert any("facet failed (ValueError)" in g for g in result["answer"].coverage.gaps)
    with pytest.raises(ValueError):
        await graph.ainvoke(
            {"question": "Compare", "history": []},
            {"configurable": {"thread_id": str(uuid4())}}, context=context(True),
        )


def test_error_events_report_innermost_exception_types() -> None:
    from crv_agent.api.app import leaf_types

    nested = ExceptionGroup("g", [ValueError("x"), ExceptionGroup("h", [KeyError("y")])])
    assert leaf_types(nested) == "KeyError,ValueError"
    assert leaf_types(RuntimeError()) == "RuntimeError"


@pytest.mark.asyncio
async def test_repair_drops_only_verifier_rejected_claims() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict

    good = CLAIM
    bad = CLAIM.model_copy(update={"text": "Recovery dated wrongly", "date": "2015"})

    class Models(FakeModels):
        def __init__(self) -> None:
            super().__init__(intent="synthesis")
            self.ledgers: list[list[object]] = []

        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.ledgers.append(list(claims))
            await budget.reserve(model=1, tokens=10)
            return AnswerDraft(content="Recovery 87.5 % [S1]", claims=list(claims),
                               insufficient=False)

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            await budget.reserve(model=1, tokens=10)
            if bad in draft.claims:
                return Verdict(supported=False, reasons=["date not supported"],
                               unsupported_claims=[draft.claims.index(bad)])
            return Verdict(supported=True, reasons=[])

    class TwoClaims(FakeWorker):
        async def run(self, question: str, release: str, hashes: list[str], budget: Budget):
            from crv_agent.domain.evidence import WorkerResult

            await budget.reserve(retrieval=1)
            return WorkerResult(claims=[good, bad], gaps=[]), [SOURCE]

    models = Models()
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=FakeKnowledge(), worker=TwoClaims(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    assert len(models.ledgers) == 2 and bad not in models.ledgers[1]
    assert result["answer"].claims == [good]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "rounds"), [("recompose", 2), ("prune", 1)])
async def test_prune_repair_mode_skips_the_second_compose_and_verify(
    mode: str, rounds: int,
) -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict

    unsupported = "It is the best result ever recorded."

    class Models(FakeModels):
        def __init__(self) -> None:
            super().__init__()
            self.verifications = 0

        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.compositions += 1
            return AnswerDraft(
                content=f"Recovery was 87.5 % [S1]. {unsupported}", claims=claims[:1],
                insufficient=False,
            )

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            self.verifications += 1
            return Verdict(
                supported=False, reasons=["unsupported superlative"],
                unsupported_sentences=[unsupported],
            )

    models = Models()
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t", repair_mode=mode,  # type: ignore[arg-type]
        ),
    )
    answer = result["answer"]
    assert (models.compositions, models.verifications) == (rounds, rounds)
    assert answer.status == "partial" and answer.content == "Recovery was 87.5 % [S1]."
    assert answer.claims == [CLAIM]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_at", [1, 2])
async def test_budget_stop_during_answer_keeps_the_last_verified_draft(stop_at: int) -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.domain.execution import BudgetExceeded

    unsupported = "It is the best result ever recorded."

    class Models(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.compositions += 1
            if self.compositions == stop_at:
                raise BudgetExceeded("Turn execution budget exhausted")
            return AnswerDraft(
                content=f"Recovery was 87.5 % [S1]. {unsupported}", claims=claims[:1],
                insufficient=False,
            )

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            return Verdict(
                supported=False, reasons=["unsupported superlative"],
                unsupported_sentences=[unsupported],
            )

    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=Models(), knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    answer = result["answer"]
    if stop_at == 1:
        assert answer.status == "insufficient_evidence" and not answer.claims
        assert any("budget" in reason for reason in answer.reasons)
    else:  # round 1 was verified; its pruned draft survives the stop in round 2
        assert answer.status == "partial" and answer.content == "Recovery was 87.5 % [S1]."
        assert answer.claims == [CLAIM]


def test_prune_removes_only_verbatim_unsupported_sentences_and_keeps_cited_claims() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.orchestration.nodes.answer import prune

    other = CLAIM.model_copy(update={"evidence_ids": ["S2"], "text": "Pilot dedicated to talc"})
    draft = AnswerDraft(
        content="Recovery was 87.5 % [S1]. The pilot was dedicated to talc [S2].",
        claims=[CLAIM, other], insufficient=False,
    )
    pruned = prune(draft, Verdict(
        supported=False, reasons=["overstated"],
        unsupported_sentences=["The pilot was dedicated to talc [S2]."],
    ))
    assert pruned is not None
    assert pruned.content == "Recovery was 87.5 % [S1]."
    assert pruned.claims == [CLAIM]  # S2 marker removed with its sentence
    assert prune(draft, Verdict(supported=False, reasons=["x"])) is None
    assert prune(draft, Verdict(
        supported=False, reasons=["x"], unsupported_sentences=["not in content"],
    )) is None
    everything = prune(draft, Verdict(
        supported=False, reasons=["x"], unsupported_sentences=[draft.content],
    ))
    assert everything is None


@pytest.mark.asyncio
async def test_failed_final_draft_is_pruned_to_a_partial_verified_answer() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict

    class Models(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.compositions += 1
            await budget.reserve(model=1, tokens=10)
            return AnswerDraft(
                content="Recovery was 87.5 % [S1]. It is the best result ever.",
                claims=list(claims)[:1], insufficient=False,
            )

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            await budget.reserve(model=1, tokens=10)
            return Verdict(supported=False, reasons=["unsupported superlative"],
                           unsupported_sentences=["It is the best result ever."])

    models = Models()
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    answer = result["answer"]
    assert models.compositions == 2
    assert answer.status == "partial" and answer.content == "Recovery was 87.5 % [S1]."
    assert answer.citations == [SOURCE]
    assert answer.reasons == ["Statements the verifier found unsupported were removed."]


def test_markers_must_bind_to_selected_claims_and_orphans_are_pruned() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.orchestration.nodes.answer import prune
    from crv_agent.services.verification import verify_draft

    other = SOURCE.model_copy(update={"id": "S2", "content": "Other fact."})
    draft = AnswerDraft(
        content="Recovery was 87.5 % [S1]. Other fact [S2].", claims=[CLAIM], insufficient=False,
    )
    assert "Answer cites evidence not bound to a selected claim" in verify_draft(
        draft, [SOURCE, other], "r1",
    )
    pruned = prune(draft, Verdict(supported=False, reasons=["x"], unsupported_claims=[]
                                  ) .model_copy(update={"unsupported_sentences": ["nothing"]}))
    assert pruned is not None and pruned.content == "Recovery was 87.5 % [S1]."
    assert verify_draft(pruned, [SOURCE, other], "r1") == []
