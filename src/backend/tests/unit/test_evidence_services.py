import asyncio
from decimal import Decimal

import pytest

from crv_agent.domain.answers import AnswerDraft
from crv_agent.domain.evidence import CalculationRequest, Claim, Evidence
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.domain.planning import Document, DocumentPage
from crv_agent.services.calculation import calculate
from crv_agent.services.catalogue import enumerate_documents
from crv_agent.services.reconciliation import conflicts
from crv_agent.services.retrieval import fuse
from crv_agent.services.verification import verify_draft


def source(**changes: object) -> Evidence:
    return Evidence.model_validate({
        "id": "S1", "release": "r1", "chunk_id": "c1", "content_hash": "hash1",
        "title": "Report", "content": "Recovery 87,5 % at pilot scale.",
        "source_uri": "raw/report.pdf", "pages": [4], "locators": [],
        "original_sources": [], "generated": False, "document_class": "report",
        **changes,
    })


def claim(**changes: object) -> Claim:
    return Claim.model_validate({
        "text": "Recovery was 87.5 %", "evidence_ids": ["S1"],
        "quote": "Recovery 87,5 % at pilot scale.", "subject": "sample A",
        "quantity": "recovery", "value": "87.5", "unit": "%", "date": None,
        "conditions": "pilot", "scope": None, **changes,
    })


def test_exact_cited_decimal_and_unknown_citations() -> None:
    draft = AnswerDraft(content="Recovery: 87.5 % [S1]", claims=[claim()], insufficient=False)
    assert verify_draft(draft, [source()], "r1") == []
    assert verify_draft(draft, [source()], "r2")
    assert verify_draft(draft.model_copy(update={"content": "[invented]"}), [source()], "r1")
    assert verify_draft(
        draft.model_copy(update={"claims": [claim(value="99")]}), [source()], "r1",
    )


def test_generated_text_and_missing_locators_cannot_ground_claims() -> None:
    draft = AnswerDraft(content="Recovery [S1]", claims=[claim()], insufficient=False)
    assert verify_draft(draft, [source(generated=True)], "r1")
    assert verify_draft(draft, [source(pages=[])], "r1")


def test_conflicts_preserve_scope_and_conditions() -> None:
    assert conflicts([claim(), claim(value="80")])
    assert not conflicts([claim(), claim(value="80", conditions="lab")])
    assert not conflicts([claim(), claim(value="80", scope="different cost basis")])
    assert not conflicts([claim(unit="kg/t", value="1"), claim(unit="g/t", value="1000")])


def test_qualified_numeric_values_are_not_silently_exact() -> None:
    evidence = source(content="Recovery < 87,5 % at pilot scale.")
    measured = claim(quote=evidence.content)
    draft = AnswerDraft(content="Recovery [S1]", claims=[measured], insufficient=False)
    assert verify_draft(draft, [evidence], "r1")
    qualified = measured.model_copy(update={"qualifier": "<"})
    assert not verify_draft(
        draft.model_copy(update={"claims": [qualified]}), [evidence], "r1",
    )
    with pytest.raises(ValueError, match="qualified"):
        calculate(
            CalculationRequest(operation="convert", input_claims=[0], target_unit="fraction"),
            [qualified],
        )


def test_calculator_units_and_invalid_inputs() -> None:
    request = CalculationRequest(operation="convert", input_claims=[0], target_unit="kWh/t")
    result = calculate(request, [claim(value="10", unit="kWh/sht")])
    assert abs(result.value - Decimal("11.023113109243879")) < Decimal("0.000000000001")
    assert result.evidence_ids == ["S1"]
    with pytest.raises(ValueError, match="incompatible"):
        calculate(request, [claim(unit="kg/t")])
    with pytest.raises(ValueError, match="existing"):
        calculate(request.model_copy(update={"input_claims": [-1]}), [claim()])
    with pytest.raises(ValueError, match="nonzero"):
        calculate(
            CalculationRequest(operation="ratio", input_claims=[0, 1], target_unit=None),
            [claim(), claim(value="0")],
        )


def test_calculator_accepts_unit_spellings_but_not_different_grades_or_dimensions() -> None:
    # Q65 shape: concentrate 840 T at 24,6 % Cu; feed 8 800 t at 2,78 %.
    ledger = [
        claim(value="840", unit="T"), claim(value="24.6", unit="% Cu"),
        claim(value="8.8", unit="kt"), claim(value="2.78", unit="%"),
    ]
    request = CalculationRequest(
        operation="weighted_recovery", input_claims=[0, 1, 2, 3], target_unit=None,
    )
    result = calculate(request, ledger)
    assert result.unit == "%" and round(result.value, 2) == Decimal("84.47")
    ratio = CalculationRequest(operation="ratio", input_claims=[0, 1], target_unit=None)
    assert calculate(ratio, [claim(value="44.46", unit="tonne"), claim(
        value="46.63", unit="Tonnes",
    )]).unit == "fraction"
    total = CalculationRequest(operation="sum", input_claims=[0, 1], target_unit=None)
    summed = calculate(total, [claim(value="1", unit="kt"), claim(value="500", unit="t")])
    assert (summed.value, summed.unit) == (Decimal("1.5"), "kt")
    with pytest.raises(ValueError, match="matching"):
        calculate(request, [*ledger[:3], claim(value="2.78", unit="% Zn")])
    with pytest.raises(ValueError, match="compatible"):
        calculate(ratio, [claim(value="840", unit="t"), claim(value="24.6", unit="%")])
    with pytest.raises(ValueError, match="compatible"):
        calculate(ratio, [claim(value="840", unit="tons"), claim(value="800", unit="t")])


def test_approximate_inputs_give_an_approximate_result_but_bounds_do_not() -> None:
    # "environ 8 800 T" is approximate; the derived recovery must say so, not refuse.
    ledger = [
        claim(value="840", unit="T"), claim(value="24.6", unit="% Cu"),
        claim(value="8800", unit="T", qualifier="~"), claim(value="2.78", unit="%"),
    ]
    request = CalculationRequest(
        operation="weighted_recovery", input_claims=[0, 1, 2, 3], target_unit=None,
    )
    result = calculate(request, ledger)
    assert result.approximate and round(result.value, 2) == Decimal("84.47")
    exact = [*ledger[:2], claim(value="8800", unit="T"), ledger[3]]
    assert not calculate(request, exact).approximate
    for bound in ("<", ">="):
        with pytest.raises(ValueError, match="bounded"):
            calculate(request, [*ledger[:2], claim(value="8800", unit="T", qualifier=bound),
                                ledger[3]])


def test_weighted_recovery_rejects_reused_claims_and_masses_used_as_grades() -> None:
    mass = [claim(value="8800", unit="T", qualifier="~"), claim(value="840", unit="T")]
    reused = CalculationRequest(
        operation="weighted_recovery", input_claims=[1, 1, 0, 0], target_unit=None,
    )
    with pytest.raises(ValueError, match="distinct"):
        calculate(reused, mass)
    ledger = [mass[1], claim(value="840", unit="T", evidence_ids=["S2"]), mass[0],
              claim(value="8800", unit="T", evidence_ids=["S3"])]
    with pytest.raises(ValueError, match="mass/grade"):
        calculate(CalculationRequest(
            operation="weighted_recovery", input_claims=[0, 1, 2, 3], target_unit=None,
        ), ledger)


def test_fusion_deduplicates_chunks_not_versions() -> None:
    other = source(id="S2", chunk_id="c2", content_hash="hash2")
    assert {item.id for item in fuse([[source(), other], [source()]])} == {"S1", "S2"}
    assert fuse([[source(generated=True)]]) == []


def test_serial_recovery_and_observed_difference_are_exact_and_cited() -> None:
    ledger = [claim(value="22.76", unit="%"), claim(value="87.52", unit="%", evidence_ids=["S2"])]
    result = calculate(CalculationRequest(
        operation="serial_recovery", input_claims=[0, 1], target_unit=None,
    ), ledger)
    assert result.value == Decimal("90.360448") and result.unit == "%"
    assert result.evidence_ids == ["S1", "S2"]
    difference = calculate(CalculationRequest(
        operation="percentage_point_difference", input_claims=[0, 1], target_unit=None,
    ), [claim(value="94.68", unit="%"), claim(value=result.value, unit="%")])
    assert difference.value == Decimal("4.319552")
    assert difference.unit == "percentage points"
    fractional = calculate(CalculationRequest(
        operation="serial_recovery", input_claims=[0, 1], target_unit=None,
    ), [claim(value="0.2276", unit="fraction"), claim(value="0.8752", unit="fraction")])
    assert fractional.value == result.value


@pytest.mark.parametrize(("value", "unit"), [("101", "%"), ("-1", "%"), ("1", "t")])
def test_serial_recovery_rejects_bad_bounds_or_dimensions(value: str, unit: str) -> None:
    with pytest.raises(ValueError, match="percentages/fractions"):
        calculate(CalculationRequest(
            operation="serial_recovery", input_claims=[0, 1], target_unit=None,
        ), [claim(value=value, unit=unit), claim(value="87.52", unit="%")])


@pytest.mark.asyncio
async def test_reconciliation_adds_serial_recovery_and_cited_observed_gap() -> None:
    from types import SimpleNamespace

    from crv_agent.domain.answers import Coverage
    from crv_agent.orchestration.nodes.research import reconcile

    class Calculator:
        async def calculation(self, question, claims, budget):
            return CalculationRequest(
                operation="serial_recovery", input_claims=[0, 1], target_unit=None,
                comparison_claim=2,
            )

    result = await reconcile({
        "claims": [claim(value="22.76"), claim(value="87.52"), claim(value="94.68")],
        "coverage": Coverage(), "question": "Verify a published recovery calculation",
        "plan": SimpleNamespace(intent="calculation"),
    }, SimpleNamespace(context=SimpleNamespace(models=Calculator(), budget=Budget())))
    assert [item.value for item in result["calculations"]] == [
        Decimal("90.360448"), Decimal("4.319552"),
    ]
    assert [item.calculation_index for item in result["claims"][-2:]] == [0, 1]
    assert [item.qualifier for item in result["claims"][-2:]] == [None, None]
    approximate = await reconcile({
        "claims": [claim(value="22.76", qualifier="~"), claim(value="87.52"), claim(value="94.68")],
        "coverage": Coverage(), "question": "Verify a published recovery calculation",
        "plan": SimpleNamespace(intent="calculation"),
    }, SimpleNamespace(context=SimpleNamespace(models=Calculator(), budget=Budget())))
    assert [item.approximate for item in approximate["calculations"]] == [True, True]
    assert [item.qualifier for item in approximate["claims"][-2:]] == ["~", "~"]


@pytest.mark.asyncio
async def test_budget_is_atomic_across_parallel_workers() -> None:
    budget = Budget(retrieval_limit=1)
    results = await asyncio.gather(
        budget.reserve(retrieval=1), budget.reserve(retrieval=1), return_exceptions=True,
    )
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    assert budget.retrieval_calls == 1


@pytest.mark.asyncio
async def test_catalogue_walks_beyond_two_hundred_documents() -> None:
    class Catalogue:
        async def documents(
            self, entities: list[str], release: str, cursor: str | None,
        ) -> DocumentPage:
            start = int(cursor or 0)
            end = min(start + 200, 405)
            return DocumentPage(
                documents=[
                    Document(
                        iri=str(i), title=str(i), content_hash=None,
                        date=None, status="observed", source_paths=[],
                    ) for i in range(start, end)
                ],
                next_cursor=str(end) if end < 405 else None, total=405,
            )

        async def resolve(self, text: str, release: str) -> list[object]:
            raise AssertionError("Not used")

        async def lineage(self, iri: str, release: str) -> list[dict[str, object]]:
            raise AssertionError("Not used")

        async def ready(self, release: str) -> None:
            raise AssertionError("Not used")

    documents, coverage = await enumerate_documents(Catalogue(), [], "r1", Budget())
    assert len(documents) == coverage.matched == coverage.enumerated == 405
    assert coverage.catalogue_complete


@pytest.mark.parametrize(("raw", "expected"), [
    ("23,8", Decimal("23.8")), ("87.5", Decimal("87.5")), ("1 234,5", Decimal("1234.5")),
    (12, Decimal(12)), ("2.7 à 2.8", None), ("deux", None), (None, None),
])
def test_claim_values_normalize_decimal_commas_and_keep_ranges_qualitative(
    raw: object, expected: Decimal | None,
) -> None:
    assert claim(value=raw).value == expected


def test_one_unparsable_value_does_not_reject_the_worker_result() -> None:
    from crv_agent.domain.evidence import WorkerResult

    result = WorkerResult.model_validate({"claims": [
        claim().model_dump(mode="json") | {"value": "2.7 à 2.8"},
        claim().model_dump(mode="json") | {"value": "23,8"},
    ], "gaps": []})
    assert [c.value for c in result.claims] == [None, Decimal("23.8")]


def test_structured_output_schemas_have_no_regex_patterns() -> None:
    """Strict provider structured outputs reject lookaround patterns (e.g. Pydantic Decimal)."""
    import json

    from crv_agent.domain.answers import AnswerDraft, Verdict
    from crv_agent.domain.evidence import CandidateRanking, WorkerResult
    from crv_agent.domain.planning import QueryPlan
    from crv_agent.evaluation.judge import FactGrades
    from crv_agent.orchestration.extraction import Extraction

    for model in (
        QueryPlan, AnswerDraft, Verdict, CalculationRequest, CandidateRanking, WorkerResult,
        Extraction, FactGrades,
    ):
        assert '"pattern"' not in json.dumps(model.model_json_schema()), model.__name__


@pytest.mark.asyncio
async def test_composer_cites_claims_and_the_citations_select_them() -> None:
    from crv_agent.adapters.models import LangChainModels
    from crv_agent.domain.answers import ComposedAnswer

    class Models(LangChainModels):
        def __init__(self) -> None:
            self.output_tokens, self.answer = 10, None

        async def _invoke(self, model, schema, system, data, budget, *, role):  # type: ignore[no-untyped-def]
            assert schema is ComposedAnswer and '"number": 1' in data and '"cite"' not in data
            assert role == "answer"
            assert '"evidence_ids"' not in data
            return ComposedAnswer(content="87.5 % [C1] [C9]", claim_numbers=[1, 7, -1],
                                  insufficient=False, unanswered=["2016 value"], conflicts=[])

    ledger = [
        claim(value="80", evidence_ids=["long-opaque-id"]),
        claim(evidence_ids=["long-opaque-id"]),
    ]
    draft = await Models().compose("Q?", ledger, "{}", Budget())
    assert draft.claims == [ledger[1]]
    assert draft.unanswered == ["2016 value"]
    assert draft.content == "87.5 % [long-opaque-id] [C9]"  # unknown claim kept, then rejected
    evidence = [source(id="long-opaque-id")]
    assert "Answer contains unknown citation markers" in verify_draft(draft, evidence, "r1")
    fixed = draft.model_copy(update={"content": "87.5 % [long-opaque-id]"})
    assert verify_draft(fixed, evidence, "r1") == []


@pytest.mark.asyncio
async def test_cited_but_unlisted_claims_are_selected_and_listed_uncited_ones_dropped() -> None:
    from crv_agent.adapters.models import LangChainModels
    from crv_agent.domain.answers import ComposedAnswer

    class Models(LangChainModels):
        def __init__(self) -> None:
            self.output_tokens, self.answer = 10, None

        async def _invoke(self, model, schema, system, data, budget, *, role):  # type: ignore[no-untyped-def]
            return ComposedAnswer(
                content="Recovery 87.5 % [C0][C2]; feed 80 % [c 1, C2].", claim_numbers=[3, 0],
                insufficient=False, unanswered=[], conflicts=[],
            )

    ledger = [
        claim(evidence_ids=["S1"]), claim(value="80", quote="Recovery 80 % at pilot scale.",
                                          evidence_ids=["S2"]),
        claim(evidence_ids=["S1"]), claim(evidence_ids=["S9"]),
    ]
    draft = await Models().compose("Q?", ledger, "{}", Budget())
    # [C0][C2] share one source: one marker. [c 1, C2] is a list. C3 is listed, never cited.
    assert draft.content == "Recovery 87.5 % [S1]; feed 80 % [S2][S1]."
    assert draft.claims == [ledger[0], ledger[2], ledger[1]]
    evidence = [source(), source(id="S2", content="Recovery 80 % at pilot scale.")]
    assert verify_draft(draft, evidence, "r1") == []


@pytest.mark.asyncio
async def test_composer_conflicts_need_two_selected_claims_that_differ() -> None:
    from crv_agent.adapters.models import LangChainModels
    from crv_agent.domain.answers import ComposedAnswer, ComposedConflict

    class Models(LangChainModels):
        def __init__(self, conflicts: list[ComposedConflict]) -> None:
            self.output_tokens, self.answer, self.conflicts = 10, None, conflicts

        async def _invoke(self, model, schema, system, data, budget, *, role):  # type: ignore[no-untyped-def]
            return ComposedAnswer(content="[C0][C1][C2]", claim_numbers=[0, 1, 2],
                                  insufficient=False, unanswered=[], conflicts=self.conflicts)

    ledger = [claim(), claim(value="80", quote="Recovery 80 % at pilot scale."), claim()]
    differing = ComposedConflict(subject="sample A", quantity="recovery", claim_numbers=[0, 1])
    same = ComposedConflict(subject="sample A", quantity="recovery", claim_numbers=[0, 2])
    unknown = ComposedConflict(subject="x", quantity="y", claim_numbers=[0, 9])
    draft = await Models([differing, same, unknown]).compose("Q?", ledger, "{}", Budget())
    assert [c.claim_indices for c in draft.conflicts] == [[0, 1]]


@pytest.mark.asyncio
async def test_a_claim_citation_shows_every_source_of_the_claim() -> None:
    from crv_agent.adapters.models import LangChainModels
    from crv_agent.domain.answers import ComposedAnswer

    class Models(LangChainModels):
        def __init__(self) -> None:
            self.output_tokens, self.answer = 10, None

        async def _invoke(self, model, schema, system, data, budget, *, role):  # type: ignore[no-untyped-def]
            return ComposedAnswer(content="87.5 % [C0].", claim_numbers=[0],
                                  insufficient=False, unanswered=[], conflicts=[])

    ledger = [claim(evidence_ids=["context", "S1"])]
    draft = await Models().compose("Q?", ledger, "{}", Budget())
    assert draft.content == "87.5 % [context][S1]."
    evidence = [source(), source(id="context", content="Le stock intermédiaire qui a un")]
    assert verify_draft(draft, evidence, "r1") == []


@pytest.mark.asyncio
async def test_verifier_sees_a_context_citation_whole_and_quoted_passages_as_windows() -> None:
    import json

    from crv_agent.adapters.models import LangChainModels
    from crv_agent.domain.answers import Verdict

    class Models(LangChainModels):
        def __init__(self) -> None:
            self.output_tokens, self.verifier, self.data = 10, None, ""

        async def _invoke(self, model, schema, system, data, budget, *, role):  # type: ignore[no-untyped-def]
            self.data = data
            return Verdict(supported=True, reasons=[], unsupported_claims=[])

    quoted = source(content="x" * 2000 + "Recovery 87,5 % at pilot scale." + "y" * 2000)
    context = source(id="context", content="z" * 3000 + " le stock intermédiaire qui a un")
    derived = claim(evidence_ids=["S1", "other"], calculation_index=0)
    other = source(id="other", content="w" * 2000 + "Recovery 87,5 % at pilot scale." + "v" * 2000)
    draft = AnswerDraft(content="[S1][context][other]", insufficient=False, claims=[
        claim(evidence_ids=["context", "S1"]), derived,
    ])
    models = Models()
    await models.verify(draft, [quoted, context, other], "{}", Budget())
    shown = {item["id"]: item["shown_text"] for item in json.loads(models.data)["sources"]}
    assert shown["context"] == [context.content]
    assert len(shown["S1"][0]) < len(quoted.content)
    assert len(shown["other"][0]) < len(other.content)  # derived claims keep focused windows


@pytest.mark.asyncio
async def test_research_reservations_keep_answer_headroom() -> None:
    budget = Budget(token_limit=100, model_limit=5)
    await budget.reserve(model=1, tokens=50, headroom=40)
    with pytest.raises(BudgetExceeded):
        await budget.reserve(model=1, tokens=20, headroom=40)  # would eat the answer reserve
    await budget.reserve(model=1, tokens=45)  # the answer stage may use the headroom
    calls = Budget(model_limit=3)
    await calls.reserve(model=1, headroom=1)
    with pytest.raises(BudgetExceeded):
        await calls.reserve(model=1, headroom=1)  # two model calls stay for answering
    await calls.reserve(model=2)


@pytest.mark.asyncio
async def test_usage_recorder_aggregates_role_served_model_cache_reasoning_and_latency() -> None:
    from uuid import uuid4

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from crv_agent.orchestration.middleware import UsageRecorder

    budget = Budget()
    recorder = UsageRecorder(budget, "worker", "evidence_extractor")
    run_id = uuid4()
    await recorder.on_chat_model_start({}, [[]], run_id=run_id)
    message = AIMessage(content="{}", usage_metadata={
        "input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
        "input_token_details": {"cache_read": 64}, "output_token_details": {"reasoning": 30},
    }, response_metadata={"model_name": "gpt-5.4-mini-2026-03-17"})
    await recorder.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id,
    )
    await budget.record_tokens(10, 5, role="worker", model="gpt-5.4-nano")
    await budget.record_tokens(1, 1)  # role-less usage still counts toward the turn budget
    usage = budget.role_usage()["worker"]
    assert usage["calls"] == 2 and usage["input_tokens"] == 110 and usage["cached_tokens"] == 64
    assert usage["reasoning_tokens"] == 30
    assert usage["models"] == {"gpt-5.4-mini-2026-03-17": 1, "gpt-5.4-nano": 1}
    assert isinstance(usage["seconds"], float) and not recorder.started
    assert budget.usage()["input_tokens"] == 111
    assert budget.usage()["cached_tokens"] == 64 and budget.usage()["reasoning_tokens"] == 30


def test_answer_numbers_must_come_from_claims_or_deterministic_calculations() -> None:
    from crv_agent.domain.evidence import Calculation
    from crv_agent.services.verification import ungrounded_numbers

    feed = claim(value="2.78", unit="% Cu", quote="8800 T à 2,78% Cu")
    concentrate = claim(value="24.6", unit="% Cu", quote="840 T de concentré à 24,6% Cu")
    llm_math = "Récupération = (840 × 0,246) / (8800 × 0,0278) = 84,47 %, soit 84,5 % [S1]."
    assert ungrounded_numbers(llm_math, [feed, concentrate], []) == [
        "0,246", "0,0278", "84,47", "84,5",
    ]
    calc = Calculation(operation="weighted_recovery", input_claims=[0, 1],
                       value=Decimal("84.4708"), unit="%", evidence_ids=["S1"])
    assert ungrounded_numbers("Récupération dérivée : 84,47 % (≈ 84,5 %) [S1].",
                              [feed, concentrate], [calc]) == []
    assert ungrounded_numbers("Teneur 2,78 % Cu en 2015, 12 échantillons [S1].", [feed], []) == []
    draft = AnswerDraft(content=llm_math, claims=[claim()], insufficient=False)
    assert any("not in cited claims" in e for e in verify_draft(draft, [source()], "r1"))


@pytest.mark.asyncio
async def test_sentences_with_llm_arithmetic_are_pruned_deterministically() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from crv_agent.adapters.postgres.memory import serializer
    from crv_agent.domain.answers import Verdict
    from crv_agent.orchestration.context import ResearchContext
    from crv_agent.orchestration.graph import build_graph
    from tests.fakes import FakeKnowledge, FakeModels, FakeWorker

    class Models(FakeModels):
        async def compose(self, question, claims, context, budget):  # type: ignore[no-untyped-def]
            await budget.reserve(model=1, tokens=10)
            return AnswerDraft(
                content="Recovery was 87.5 % at pilot scale [S1].\nImplied loss is 12.5 % [S1].",
                claims=list(claims)[:1], insufficient=False,
            )

        async def verify(self, draft, evidence, context, budget):  # type: ignore[no-untyped-def]
            await budget.reserve(model=1, tokens=10)
            return Verdict(supported=True, reasons=[])

    from uuid import uuid4

    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=Models(), knowledge=FakeKnowledge(), worker=FakeWorker(), budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="t",
        ),
    )
    answer = result["answer"]
    assert answer.status == "answered"  # an unsupported sentence went, every sourced fact stayed
    assert answer.content == "Recovery was 87.5 % at pilot scale [S1]."
