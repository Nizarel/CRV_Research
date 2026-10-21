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


def test_fusion_deduplicates_chunks_not_versions() -> None:
    other = source(id="S2", chunk_id="c2", content_hash="hash2")
    assert {item.id for item in fuse([[source(), other], [source()]])} == {"S1", "S2"}
    assert fuse([[source(generated=True)]]) == []


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
