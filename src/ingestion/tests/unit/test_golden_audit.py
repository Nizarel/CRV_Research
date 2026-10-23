"""Extraction audit buckets and the status review sheet (offline, fake LLM client)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from ingest_pipeline.tools.golden.audit import (
    audit_run,
    classify,
    fact_stage,
    normalize,
    rescues,
)
from ingest_pipeline.tools.golden.dataset import GoldenQuestion, KeyFact
from ingest_pipeline.tools.golden.status_review import build_rows, select

QUESTION = GoldenQuestion(
    id="Q1",
    number=1,
    question="Recovery and tonnage of the pilot?",
    expected="Recovery 87,5 % ; 8 800 t treated",
    category="A",
    key_facts=(
        KeyFact(text="Recovery 87,5 %", anchors=("#87.5",)),
        KeyFact(text="8 800 t treated", anchors=("#8800",)),
    ),
    split="dev",
)
PASSAGES = {
    "S1": "Pilot results:\nRecovery\u00a087,5 % at d80 120 \u00b5m.",
    "S2": "Campaign: 8 800 t treated at the plant.",
}


def claim(
    quote: str,
    outcome: str,
    errors: list[str] | None = None,
    value: str | None = None,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "text": "c",
        "quote": quote,
        "value": value,
        "evidence_ids": ids or ["S1"],
        "outcome": outcome,
        "errors": errors or [],
    }


def audit(
    *claims: dict[str, Any], seen: list[str] | None = None, status: str = "completed"
) -> list[dict[str, Any]]:
    return [
        {
            "facet": "Recovery?",
            "status": status,
            "claims": list(claims),
            "rounds": [
                {
                    "query": "q",
                    "passage_ids": seen or ["S0", "S1", "S2"],
                    "gaps": [],
                    "rewrite": None,
                }
            ],
        }
    ]


def test_normalized_matching_rescues_whitespace_and_grouped_numbers_only() -> None:
    assert normalize("Recovery\u00a087,5\u202f%\n at") == "Recovery 87,5 % at"
    quoted = claim(
        "Recovery 87,5 % at d80",
        "rejected",
        ["Claim requires an exact quotation from source text shown to the model"],
    )
    assert rescues(quoted, PASSAGES) == (True, None)
    grouped = claim(
        "8 800 t treated",
        "rejected",
        ["Claim numeric value/qualifier does not occur in its quotation"],
        value="8800",
        ids=["S2"],
    )
    assert rescues(grouped, PASSAGES) == (None, True)
    invented = claim(
        "Recovery 91 %",
        "rejected",
        ["Claim requires an exact quotation from source text shown to the model"],
    )
    assert rescues(invented, PASSAGES) == (False, None)


def test_fact_stage_follows_the_exposed_ledger_answer_funnel() -> None:
    scores = {
        "AUD-EXPOSED": {"judge_recall": 0.5, "unsupported": [2]},
        "AUD-LEDGER": {"judge_recall": 0.0, "unsupported": [1, 2]},
        "AUD": {"judge_recall": 0.0, "unsupported": [1, 2]},
    }
    assert fact_stage(1, scores, "AUD") == "lost_at_extraction"
    assert fact_stage(2, scores, "AUD") == "not_exposed"
    assert fact_stage(1, {}, "AUD") is None


def test_classify_buckets_lost_facts_by_their_matching_claims_and_passages() -> None:
    rejected = claim(
        "Recovery 87,5 % at d80",
        "rejected",
        ["Claim requires an exact quotation from source text shown to the model"],
    )
    assert (
        classify(QUESTION, 1, audit(claim("Recovery 87,5 %", "accepted")), PASSAGES).bucket
        == "judge_noise"
    )
    lost = classify(QUESTION, 1, audit(rejected), PASSAGES)
    assert (lost.bucket, lost.detail, lost.normalized_quote_match) == ("validation", "quote", True)
    assert (
        classify(QUESTION, 1, audit(claim("Recovery 87,5 %", "capped")), PASSAGES).bucket == "cap"
    )
    skipped = classify(QUESTION, 1, audit(), PASSAGES)
    assert (skipped.bucket, skipped.passages, skipped.best_rank, skipped.shown) == (
        "not_extracted",
        ["S1"],
        2,
        3,
    )
    assert classify(QUESTION, 1, audit(), PASSAGES, scope=[1]).bucket == "extractor_skip"
    assert classify(QUESTION, 1, audit(), PASSAGES, scope=[]).bucket == "facet_scope"
    assert classify(QUESTION, 1, audit(status="failed"), PASSAGES).bucket == "facet_failed"
    assert classify(QUESTION, 1, audit(), {"S9": "unrelated"}).bucket == "unlocated"


def test_audit_run_counts_stages_buckets_and_claim_rescues() -> None:
    row = {
        "id": "Q1",
        "arm": "AUD",
        "audit": audit(
            claim(
                "8 800 t treated",
                "rejected",
                ["Claim numeric value/qualifier does not occur in its quotation"],
                value="8800",
                ids=["S2"],
            )
        ),
    }
    stages = [
        {
            "id": "Q1",
            "arm": "AUD-EXPOSED",
            "contexts": [{"id": pid, "text": text} for pid, text in PASSAGES.items()],
        }
    ]
    scores = [
        {"id": "Q1", "arm": "AUD-EXPOSED", "judge_recall": 1.0, "unsupported": []},
        {"id": "Q1", "arm": "AUD-LEDGER", "judge_recall": 0.0, "unsupported": [1, 2]},
        {"id": "Q1", "arm": "AUD", "judge_recall": 0.0, "unsupported": [1, 2]},
    ]
    lost, summary = asyncio.run(audit_run({"Q1": QUESTION}, [row], stages, scores, "AUD"))
    assert [(item.fact, item.bucket) for item in lost] == [(1, "not_extracted"), (2, "validation")]
    assert summary["stages"]["ALL"] == {"lost_at_extraction": 2}
    assert summary["claims"] == {
        "outcomes": {"rejected": 1},
        "rejections": {"number": 1},
        "rescuable": {"number_normalized": 1},
    }


class FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.prompts.append(kwargs["messages"][1]["content"])
        content = json.dumps({"label": "partly", "premise_false": False, "rationale": "r"})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_status_review_selects_insufficient_with_claims_abstentions_and_controls() -> None:
    abstain = QUESTION.model_copy(update={"id": "Q60", "category": "F", "must_abstain": True})
    questions = {"Q1": QUESTION, "Q2": QUESTION.model_copy(update={"id": "Q2"}), "Q60": abstain}
    with_claims = {"content": "Recovery 87,5 % [S1]", "claims": [{"text": "c"}]}
    rows = [
        {"id": "Q1", "arm": "PR", "status": "insufficient_evidence", "answer": with_claims},
        {"id": "Q2", "arm": "PR", "status": "insufficient_evidence", "answer": {"claims": []}},
        {"id": "Q2", "arm": "P18", "status": "partial", "answer": with_claims},
        {"id": "Q60", "arm": "PR", "status": "insufficient_evidence", "answer": {"claims": []}},
    ]
    assert [(reason, row["id"], row["arm"]) for reason, row in select(questions, rows, 1)] == [
        ("insufficient_with_claims", "Q1", "PR"),
        ("must_abstain", "Q60", "PR"),
        ("partial_control", "Q2", "P18"),
    ]
    client = FakeClient()
    sheet = asyncio.run(build_rows(questions, rows, {("Q1", "PR"): 0.5}, 0, client, "judge"))
    assert [(r["id"], r["judge_recall"], r["llm_label"]) for r in sheet] == [
        ("Q1", 0.5, "partly"),
        ("Q60", None, "partly"),
    ]
    assert "KEY FACTS:\n- Recovery 87,5 %" in client.prompts[0]
