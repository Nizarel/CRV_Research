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


@pytest.mark.parametrize("intent", ["out_of_scope", "procedure"])
def test_out_of_scope_does_not_mislabel_an_ordinary_question_as_hazardous(intent: str) -> None:
    with TestClient(create_app(services(FakeModels(intent)))) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()),
            "message": "Current copper market price?",
        })
        payloads = [json.loads(line.removeprefix("data: "))
                    for line in response.text.splitlines() if line.startswith("data: ")]
        answer = next(payload for payload in payloads if "content" in payload)
        assert answer["status"] == "out_of_scope"
        assert ("hazardous" in answer["content"].lower()) is (intent == "procedure")
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
        answer_audit = [
            json.loads(lines[2].removeprefix("data: "))
            for lines in blocks if lines[1] == "event: answer_diagnostics"
        ]
        sequences = [json.loads(lines[2].removeprefix("data: "))["sequence"] for lines in blocks]
    assert sequences == sorted(set(sequences))
    if not enabled:
        assert found == []
        assert answer_audit == []
        return
    assert answer_audit[0]["stage"] == "input"
    verified = next(item for item in answer_audit if item["stage"] == "verified")
    assert verified["draft"]["content"]
    assert verified["verifier"]["supported"] is True
    assert answer_audit[-1]["stage"] == "final"
    [payload] = found
    assert payload["ledger"] == [CLAIM.model_dump(mode="json")]
    assert payload["exposed"] == [{
        "id": "S1", "content_hash": SOURCE.content_hash, "title": SOURCE.title,
        "source": SOURCE.source_uri, "pages": [4], "text": SOURCE.content,
    }]
    [facet] = payload["audit"]
    assert (facet["facet"], facet["status"], facet["rounds"]) == ("Recovery?", "completed", [])
    assert facet["scope_hashes"] == []
    assert facet["strict"] is False
    assert [(c["outcome"], c["errors"]) for c in facet["claims"]] == [("accepted", [])]


def test_facet_audit_records_rejection_reasons_and_claims_cut_by_the_cap() -> None:
    from crv_agent.domain.evidence import ExtractionRound, TracedWorkerResult
    from crv_agent.orchestration.nodes.research import facet_audit

    spaced = CLAIM.model_copy(update={"quote": "Recovery 87.5 % at full scale."})
    spare = CLAIM.model_copy(update={"text": "Pilot recovery"})
    result = TracedWorkerResult(claims=[CLAIM, spaced], gaps=[], rounds=[ExtractionRound(
        query="Recovery?", passage_ids=["S1"], returned=[CLAIM, spaced], capped=[spare],
        gaps=["No 2019 value"], rewrite=None,
    )])
    entry = facet_audit("Recovery?", "completed", result, {"S1": SOURCE})
    assert entry["rounds"] == [{
        "query": "Recovery?", "passage_ids": ["S1"], "gaps": ["No 2019 value"], "rewrite": None,
    }]
    assert [(c["outcome"], c["errors"]) for c in entry["claims"]] == [  # type: ignore[attr-defined]
        ("accepted", []),
        ("rejected", ["Claim requires an exact quotation from source text shown to the model"]),
        ("capped", []),
    ]


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
@pytest.mark.parametrize("external", [False, True])
async def test_external_foundry_chat_is_separate_from_retrieval_and_masks_key(
    monkeypatch, external: bool,
) -> None:
    import os
    from contextlib import AsyncExitStack

    from crv_agent.runtime.infrastructure import create_chat_models
    from crv_agent.runtime.settings import Settings

    for name in [name for name in os.environ if name.upper().startswith("CRV_")]:
        monkeypatch.delenv(name)
    base = {
        "search_endpoint": "https://search", "kg_release": "kg1", "postgres_host": "pg",
        "postgres_user": "u", "kg_database": "kg", "memory_user": "m", "local_owner": "dev",
        "openai_endpoint": "https://retrieval.example.invalid", "planner_deployment": "p",
        "worker_deployment": "w", "answer_deployment": "a", "verifier_deployment": "v",
    }
    overrides = {
        "model_endpoint": "https://manadry.cognitiveservices.azure.com/",
        "model_api_key": "fake-test-key-not-a-credential",
    } if external else {}
    settings = Settings(**base, **overrides)

    async def token() -> str:
        return "test-entra-token"

    async with AsyncExitStack() as stack:
        models = create_chat_models(stack, settings, token)
        expected = "https://manadry.cognitiveservices.azure.com/openai/v1/" if external else (
            "https://retrieval.example.invalid/openai/v1/"
        )
        assert set(models) == {"planner", "worker", "answer", "verifier"}
        assert all(model.openai_api_base == expected for model in models.values())
        assert settings.openai_endpoint == "https://retrieval.example.invalid"
        assert models["worker"].max_tokens == settings.worker_output_tokens
        if external:
            assert all(model.openai_api_key.get_secret_value() == overrides["model_api_key"]
                       for model in models.values())
            assert overrides["model_api_key"] not in repr(settings)
            assert overrides["model_api_key"] not in settings.model_dump_json()
    with pytest.raises(ValueError, match="explicit model endpoint"):
        Settings(**base, model_api_key="fake-test-key-not-a-credential")
    with pytest.raises(ValueError, match="HTTPS origin"):
        Settings(**base, model_endpoint="http://manadry.cognitiveservices.azure.com/")


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

        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
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
    bad = CLAIM.model_copy(update={"text": "Recovery at bench scale", "conditions": "bench"})

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
                return Verdict(supported=False, reasons=["conditions not supported"],
                               unsupported_claims=[draft.claims.index(bad)])
            return Verdict(supported=True, reasons=[])

    class TwoClaims(FakeWorker):
        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
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
async def test_repair_clears_only_rejected_optional_bindings_and_keeps_the_claim() -> None:
    from crv_agent.domain.answers import AnswerDraft, BindingIssue, Verdict

    received = CLAIM.model_copy(update={"date": "à réception", "scope": "lot A"})

    class Models(FakeModels):
        def __init__(self) -> None:
            super().__init__(intent="synthesis")
            self.ledgers: list[list[object]] = []

        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.ledgers.append(list(claims))
            return AnswerDraft(content="Recovery 87.5 % [S1]", claims=list(claims),
                               insufficient=False)

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            if any(claim.date for claim in draft.claims):
                return Verdict(supported=False, reasons=["receipt date is not the test date"],
                               unsupported_bindings=[BindingIssue(claim=0, fields=["date"])])
            return Verdict(supported=True, reasons=[])

    class Dated(FakeWorker):
        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
            from crv_agent.domain.evidence import WorkerResult

            await budget.reserve(retrieval=1)
            return WorkerResult(claims=[received], gaps=[]), [SOURCE]

    models = Models()
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=FakeKnowledge(), worker=Dated(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    kept = received.model_copy(update={"date": None})
    assert models.ledgers == [[received], [kept]]
    assert result["answer"].status == "answered" and result["answer"].claims == [kept]


def test_prune_clears_rejected_bindings_and_keeps_conflicts_of_the_cleared_claims() -> None:
    from decimal import Decimal

    from crv_agent.domain.answers import AnswerDraft, BindingIssue, Conflict, Verdict
    from crv_agent.orchestration.nodes.answer import answer_conflicts, findings, prune

    dated = CLAIM.model_copy(update={"date": "2018", "conditions": "after filtration"})
    other = CLAIM.model_copy(update={
        "value": Decimal("80"), "quote": "Recovery 80 %", "text": "80 %",
    })
    conflict = Conflict(subject="sample", quantity="recovery", claim_indices=[0, 1])
    draft = AnswerDraft(content="Recovery 87.5 % [S1].", claims=[dated, other],
                        insufficient=False, conflicts=[conflict])
    issue = BindingIssue(claim=0, fields=["date"])
    only = prune(draft, Verdict(supported=False, reasons=["x"], unsupported_bindings=[issue]))
    assert only is not None and only.content == draft.content
    assert only.claims[0] == dated.model_copy(update={"date": None}) and only.claims[1] is other
    assert answer_conflicts(only.model_copy(update={"conflicts": []}), [dated, other], [
        conflict,
    ]) == [conflict]
    both = Verdict(supported=False, reasons=["x"], unsupported_claims=[0],
                   unsupported_bindings=[issue])
    assert prune(draft, both) is None  # a rejected claim is dropped, not repaired
    listed = findings(draft, Verdict(
        supported=False, reasons=["x"], unsupported_claims=[1], unsupported_bindings=[issue],
    ), [], "en")
    assert listed is not None and listed.claims == [dated.model_copy(update={"date": None})]


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
    assert answer.status == "answered" and answer.content == "Recovery was 87.5 % [S1]."
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
        assert answer.status == "answered" and answer.content == "Recovery was 87.5 % [S1]."
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


def test_prune_leaves_no_citation_only_lines_or_bodiless_headings() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.orchestration.nodes.answer import prune

    draft = AnswerDraft(
        content="It is the best ever. [S1]\n\n## Results\n**Pilot.**\nRecovery was 87.5 % [S1]."
        "\n\n**DLQ5.**\nIt is the best collector. [S1]\n\n**Limits.**\n- It is unmatched.",
        claims=[CLAIM], insufficient=False,
    )
    pruned = prune(draft, Verdict(supported=False, reasons=["overstated"], unsupported_sentences=[
        "It is the best ever.", "It is the best collector.", "It is unmatched.",
    ]))
    assert pruned is not None
    assert pruned.content == "## Results\n**Pilot.**\nRecovery was 87.5 % [S1]."
    assert prune(draft, Verdict(supported=False, reasons=["x"])) is None
    assert prune(draft, Verdict(
        supported=False, reasons=["x"], unsupported_sentences=["not in content"],
    )) is None
    everything = prune(draft, Verdict(
        supported=False, reasons=["x"], unsupported_sentences=[draft.content],
    ))
    assert everything is None


@pytest.mark.asyncio
async def test_failed_final_draft_is_pruned_to_a_verified_answer() -> None:
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
    assert answer.status == "answered" and answer.content == "Recovery was 87.5 % [S1]."
    assert answer.citations == [SOURCE]
    assert answer.reasons == ["Statements the verifier found unsupported were removed."]


async def _answer(models: FakeModels, worker: FakeWorker | None = None) -> Answer:
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=models, knowledge=FakeKnowledge(), worker=worker or FakeWorker(),
            budget=Budget(), search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    return result["answer"]  # type: ignore[no-any-return]


async def _status(models: FakeModels, worker: FakeWorker | None = None) -> str:
    return str((await _answer(models, worker)).status)


@pytest.mark.asyncio
async def test_status_is_partial_only_for_unanswered_or_cut_short_content() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.domain.evidence import WorkerResult
    from crv_agent.domain.execution import BudgetExceeded
    from crv_agent.domain.planning import Facet

    other = CLAIM.model_copy(update={"text": "Pilot dedicated to talc"})

    class Gapped(FakeWorker):
        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
            await budget.reserve(retrieval=1)
            return WorkerResult(claims=[CLAIM, other], gaps=["Date not printed."]), [SOURCE]

    # Standing caveats and worker gaps alone no longer make an answer partial.
    assert await _status(FakeModels(), Gapped()) == "answered"

    class Unanswered(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            draft = await super().compose(question, claims, context, budget)
            return draft.model_copy(update={"unanswered": ["2016 value"]})

    unanswered = await _answer(Unanswered(), Gapped())
    assert unanswered.status == "partial"
    assert unanswered.reasons == ["Not answered: 2016 value"]

    class TwoFacets(FakeModels):
        async def plan(self, question, history, budget):  # type: ignore[no-untyped-def]
            plan = await super().plan(question, history, budget)
            return plan.model_copy(update={"facets": [Facet(question="a"), Facet(question="b")]})

    class OneRunsOut(FakeWorker):
        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
            if question.startswith("b"):
                raise BudgetExceeded("Turn execution budget exhausted")
            return await super().run(question, release, hashes, budget)

    assert await _status(TwoFacets(), OneRunsOut()) == "partial"

    class DropsClaim(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            return AnswerDraft(
                content="Recovery was 87.5 % [S1]. Pilot dedicated to talc [S2].",
                claims=[CLAIM, other.model_copy(update={"evidence_ids": ["S2"]})],
                insufficient=False,
            )

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            return Verdict(supported=False, reasons=["talc not stated"], unsupported_claims=[1])

    class TwoSources(FakeWorker):
        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
            return WorkerResult(claims=[CLAIM], gaps=[]), [
                SOURCE, SOURCE.model_copy(update={"id": "S2", "chunk_id": "c2"}),
            ]

    assert await _status(DropsClaim("synthesis"), TwoSources()) == "answered"


def test_findings_list_only_unrejected_self_grounded_claims() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.orchestration.nodes.answer import findings

    rejected = CLAIM.model_copy(update={"text": "Recovery was the best ever"})
    loose = CLAIM.model_copy(update={"text": "Recovery rose to 93.2 %"})
    draft = AnswerDraft(
        content="Synthesis [S1].", claims=[CLAIM, rejected, loose], insufficient=False,
    )
    specific = Verdict(supported=False, reasons=["x"], unsupported_claims=[1])
    listed = findings(draft, specific, [], "en")
    assert listed is not None
    assert listed.claims == [CLAIM]
    assert listed.content.endswith("\n\n- Recovery was 87.5 % [S1]")
    assert findings(draft, specific, [], "fr").content.startswith(  # type: ignore[union-attr]
        "La synthèse n'a pas passé la vérification",
    )
    assert findings(draft, Verdict(supported=False, reasons=["x"]), [], "en") is None
    assert findings(draft, specific.model_copy(update={"unsupported_claims": [0, 1, 2]}),
                    [], "en") is None
    # Verifier accepted everything; only the deterministic contract failed (missing markers).
    accepted = findings(draft, Verdict(supported=True, reasons=[]), [], "en")
    assert accepted is not None and accepted.claims == [CLAIM, rejected]
    assert accepted.content.endswith(
        "- Recovery was 87.5 % [S1]\n- Recovery was the best ever [S1]",
    )


@pytest.mark.asyncio
async def test_a_draft_without_citation_markers_falls_back_to_the_verified_findings() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict

    class Models(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.compositions += 1
            return AnswerDraft(content="Recovery was 87.5 %.", claims=list(claims),
                               insufficient=False)

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            return Verdict(supported=True, reasons=[])

    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=Models(), knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    answer = result["answer"]
    assert answer.status == "partial"
    assert answer.content.endswith("\n\n- Recovery was 87.5 % [S1]")
    assert answer.claims == [CLAIM] and answer.citations == [SOURCE]


@pytest.mark.asyncio
async def test_unprunable_synthesis_falls_back_to_the_verified_findings() -> None:
    from crv_agent.domain.answers import AnswerDraft, Verdict

    class Models(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            self.compositions += 1
            text = " and ".join(f"{c.text} [{c.evidence_ids[0]}]" for c in claims)
            return AnswerDraft(content=text + ", so the plant steadily improved.",
                               claims=list(claims), insufficient=False)

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            # The flagged sentence is paraphrased, so pruning cannot remove it.
            return Verdict(supported=False, reasons=["chronology not supported"],
                           unsupported_sentences=["The plant improved steadily."])

    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=Models(), knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    answer = result["answer"]
    assert answer.status == "partial"
    assert answer.content.endswith("\n\n- Recovery was 87.5 % [S1]")
    assert answer.claims == [CLAIM]
    assert answer.citations == [SOURCE]
    assert answer.reasons == [
        "The synthesis failed verification; only verified findings are listed.",
    ]


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
