from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from crv_agent.api.app import create_app
from crv_agent.domain.answers import Answer, Coverage
from crv_agent.domain.evidence import CalculationRequest
from crv_agent.evaluation.dataset import GoldenQuestion, KeyFact, load_questions
from crv_agent.evaluation.gates import gate_failures
from crv_agent.evaluation.runner import evaluate, measured_rate
from crv_agent.evaluation.scoring import score
from crv_agent.services.calculation import calculate
from crv_agent.services.policy import hazardous_instructions
from crv_agent.services.verification import claim_errors
from tests.fakes import CLAIM, SOURCE, FakeModels
from tests.unit.test_graph_api import services


def test_missing_source_and_numeric_ground_truth_are_not_success() -> None:
    question = GoldenQuestion(
        id="Q", question="Recovery?", category="A", split="dev", key_facts=[],
    )
    answer = Answer(
        status="answered", content="87.5 % [S1]", claims=[CLAIM], citations=[SOURCE],
        conflicts=[], calculations=[], coverage=Coverage(), reasons=[], search_release="r1",
        kg_release="kg1", trace_id="t", usage={},
    )
    result = score(question, answer)
    assert result["numeric_exact"] is None
    assert result["source_match"] is None
    assert result["key_fact_recall"] is None
    assert measured_rate([None]) == {"rate": None, "measured": 0, "unmeasured": 1}


def test_existing_golden_split_remains_intact() -> None:
    path = Path(__file__).resolve().parents[4] / "golden" / "crv_golden.jsonl"
    if not path.exists():
        pytest.skip("The optional CRV golden corpus is not present")
    dev, heldout = load_questions(path, "dev"), load_questions(path, "heldout")
    assert len(dev) == 53 and len(heldout) == 23
    assert not {q.id for q in dev} & {q.id for q in heldout}


def test_derived_claim_requires_exact_deterministic_result() -> None:
    result = calculate(
        CalculationRequest(operation="convert", input_claims=[0], target_unit="fraction"), [CLAIM],
    )
    derived = CLAIM.model_copy(update={
        "calculation_index": 0, "value": result.value, "unit": "fraction",
    })
    assert result.value == Decimal("0.875")
    assert not claim_errors(derived, {"S1": SOURCE}, [result])
    assert claim_errors(derived.model_copy(update={"value": Decimal(4)}), {"S1": SOURCE}, [result])
    assert claim_errors(derived, {"S1": SOURCE})


def test_missing_gates_never_pass_and_progress_is_not_answer_latency() -> None:
    failures = gate_failures({}, 0.01)
    assert "source_match: not measured" in failures
    assert "key_fact_recall_A: not measured" in failures
    assert gate_failures({"citation_contract_valid": [True]}, 26)


def test_hazardous_protocols_are_blocked_before_model_planning() -> None:
    assert hazardous_instructions("Comment préparer une solution de cyanure ?")
    assert hazardous_instructions("How to prepare NaCN?")
    assert not hazardous_instructions("Which report mentions cyanidation?")


@pytest.mark.asyncio
async def test_evaluator_consumes_verified_api_answers_and_reports_missing_gates() -> None:
    app = create_app(services(FakeModels()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        report = await evaluate(client, [GoldenQuestion(
            id="Q", question="Recovery?", category="A", split="dev",
            key_facts=[KeyFact(text="Recovery 87.5 %")],
        )])
    assert report["questions"] == 1
    assert report["gates_passed"] is False


@pytest.mark.asyncio
async def test_failed_turns_are_recorded_and_scored_as_misses_not_dropped() -> None:
    from crv_agent.domain.execution import Budget

    class Failing(FakeModels):
        async def plan(self, question: str, history: list[str], budget: Budget):
            raise ConnectionError("provider down")

    class Judge:
        async def grade(self, facts: list[str], answer: str) -> list[bool]:
            return [True] * len(facts)

    exported: list[dict[str, object]] = []
    app = create_app(services(Failing()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        report = await evaluate(client, [
            GoldenQuestion(id="Q1", question="Recovery?", category="A", split="dev",
                           key_facts=[KeyFact(text="Recovery 87.5 %")]),
            GoldenQuestion(id="Q60", question="Prix 2025 ?", category="F", split="dev",
                           key_facts=[], must_abstain=True),
        ], Judge(), concurrency=2, emit=exported.append)
    assert report["questions"] == 2
    assert report["statuses"] == {"error": 2}
    assert report["turn_errors"] == {"ConnectionError": 2}
    assert report["metrics"]["turn_completed"]["rate"] == 0  # type: ignore[index]
    assert report["metrics"]["key_fact_recall_A"]["rate"] == 0  # type: ignore[index]
    assert report["metrics"]["abstention_correct"]["rate"] == 0  # type: ignore[index]
    assert report["first_answer_seconds_p95"] is not None
    assert [r["id"] for r in report["records"]] == ["Q1", "Q60"]  # type: ignore[index]
    assert {e["status"] for e in exported} == {"error"}
    assert all(e["contexts"][0]["text"] == "" for e in exported)  # type: ignore[index]


@pytest.mark.asyncio
async def test_answer_export_matches_shared_score_context_format_without_source_text() -> None:
    exported: list[dict[str, object]] = []
    app = create_app(services(FakeModels()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        report = await evaluate(client, [GoldenQuestion(
            id="Q2", question="Recovery?", category="A", split="dev",
            key_facts=[KeyFact(text="Recovery 87.5 %")],
        )], emit=exported.append)
    [record] = exported
    assert report["metrics"]["turn_completed"]["rate"] == 1  # type: ignore[index]
    assert record["arm"] == "AGENT" and record["passage_mode"] == "split"
    assert record["error"] is None and record["status"] in {"answered", "partial"}
    assert record["contexts"] == [{  # Only the answer text is judged.
        "id": "answer", "content_hash": "", "title": "answer", "source": "answer",
        "pages": [], "text": "Recovery was 87.5 % [S1]",
    }]
    citation = record["answer"]["citations"][0]  # type: ignore[index]
    assert citation["pages"] == [4] and "content" not in citation


@pytest.mark.asyncio
async def test_judge_failures_leave_questions_unmeasured_without_aborting_the_run() -> None:
    from typing import Any

    from crv_agent.evaluation.judge import FactGrade, FactGrades, JudgeError, LangChainJudge

    class Model:
        def __init__(self, replies: list[FactGrades]) -> None:
            self.replies = replies

        def with_structured_output(self, *args: Any, **kwargs: Any) -> "Model":
            return self

        async def ainvoke(self, messages: Any) -> FactGrades:
            return self.replies.pop(0)

    wrong = FactGrades(grades=[FactGrade(index=1, matched=True)])
    right = FactGrades(grades=[FactGrade(index=2, matched=False), FactGrade(index=1, matched=True)])
    assert await LangChainJudge(Model([wrong, right])).grade(["a", "b"], "x") == [True, False]  # type: ignore[arg-type]
    with pytest.raises(JudgeError):
        await LangChainJudge(Model([wrong, wrong, wrong])).grade(["a", "b"], "x")  # type: ignore[arg-type]

    class Broken:
        async def grade(self, facts: list[str], answer: str) -> list[bool]:
            raise JudgeError("no")

    app = create_app(services(FakeModels()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        report = await evaluate(client, [GoldenQuestion(
            id="Q3", question="Recovery?", category="A", split="dev",
            key_facts=[KeyFact(text="Recovery 87.5 %")],
        )], Broken())
    assert report["records"][0]["judge_error"] == "JudgeError"  # type: ignore[index]
    assert report["metrics"]["key_fact_recall_A"] == {  # type: ignore[index]
        "rate": None, "measured": 0, "unmeasured": 1,
    }
