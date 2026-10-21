from decimal import Decimal

from crv_agent.domain.answers import Answer, AnswerDraft
from crv_agent.evaluation.dataset import GoldenQuestion
from crv_agent.services.verification import verify_draft


def score(question: GoldenQuestion, answer: Answer) -> dict[str, bool | float | None]:
    numeric: bool | None = None
    if question.numeric_checks:
        numeric = all(any(
            claim.value is not None and claim.unit == check.unit
            and abs(claim.value - Decimal(check.value)) <= Decimal(check.tolerance)
            for claim in answer.claims
        ) for check in question.numeric_checks)
    sources: bool | None = None
    if question.expected_sources:
        actual = {
            value for evidence in answer.citations
            for value in [evidence.source_uri, evidence.content_hash, *evidence.original_sources]
        }
        sources = bool(actual & set(question.expected_sources))
    errors = verify_draft(
        AnswerDraft(
            content=answer.content, claims=answer.claims,
            insufficient=answer.status != "answered",
        ), answer.citations, answer.search_release, answer.calculations,
    )
    return {
        "citation_contract_valid": not errors,
        "source_match": sources, "numeric_exact": numeric,
        "abstention_correct": answer.status == "insufficient_evidence"
        if question.must_abstain else None,
        "conflict_flagged": bool(answer.conflicts) if question.must_flag_conflict else None,
        "calculator_used": bool(answer.calculations) if question.category == "G" else None,
        "catalogue_used": answer.coverage.catalogue_complete
        if question.category == "E" else None,
        "key_fact_recall": None,
    }
