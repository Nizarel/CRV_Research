"""Shared scoring of agent contexts: markers, passage splitting, majority vote, paired CIs."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ingest_pipeline.tools.golden.dataset import GoldenQuestion, KeyFact
from ingest_pipeline.tools.golden.judge import PASSAGE_CHARS
from ingest_pipeline.tools.golden.score import (
    CONTEXTS_BEGIN,
    CONTEXTS_END,
    bootstrap,
    hits_of,
    majority,
    read_records,
    score_record,
    split_text,
    summarize,
)

QUESTION = GoldenQuestion(
    id="Q1",
    number=1,
    question="Recovery?",
    expected="87,5 % ; CR-AR-PE-01-15",
    category="A",
    key_facts=(
        KeyFact(text="Recovery 87,5 %", anchors=("#87.5",)),
        KeyFact(text="Report CR-AR-PE-01-15", anchors=("cr ar pe 01 15",)),
    ),
    split="dev",
)


def record(text: str, mode: str = "split", arm: str = "P16") -> dict[str, Any]:
    return {
        "id": "Q1",
        "category": "A",
        "split": "dev",
        "arm": arm,
        "passage_mode": mode,
        "error": None,
        "seconds": 1.0,
        "usage": {"model_calls": 0, "retrieval_calls": 2},
        "exposed_characters": len(text),
        "documents": 1,
        "secondary_share": 0.0,
        "dossier_cu_share": 1.0,
        "contexts": [
            {
                "id": "S1",
                "content_hash": "a" * 64,
                "title": "T",
                "source": "x.pdf",
                "pages": [3],
                "text": text,
            }
        ],
    }


def test_read_records_tolerates_logs_and_markers() -> None:
    lines = ["log line", CONTEXTS_BEGIN, json.dumps(record("x")), CONTEXTS_END, "{ignored"]
    assert [r["arm"] for r in read_records(lines)] == ["P16"]
    assert len(read_records([json.dumps(record("x"))])) == 1


def test_split_keeps_all_text_while_truncate_matches_golden_eval() -> None:
    text = ("mot " * 1000) + "Recovery 87,5 %"
    pieces = split_text(text)
    assert "".join(pieces) == text
    assert all(len(p) <= PASSAGE_CHARS for p in pieces)
    full, passages = hits_of(record(text))
    assert len(full) == 1
    assert len(passages) == len(pieces) > 1
    full, passages = hits_of(record(text, "truncate"))
    assert passages == full
    assert passages[0].content == text


def test_majority_vote_and_agreement() -> None:
    runs = [
        [{"supported": True}, {"supported": False}],
        [{"supported": True}, {"supported": True}],
        [{"supported": False}, {"supported": False}],
    ]
    assert majority(runs) == ([True, False], 0.0)
    assert majority([]) == ([], None)


def test_score_record_uses_golden_anchors_and_repeated_judge() -> None:
    class Judge:
        calls = 0

        async def judge(self, question: GoldenQuestion, hits: Any) -> list[dict[str, Any]]:
            Judge.calls += 1
            return [
                {"fact": 1, "supported": True, "passage": "S1~0"},
                {"fact": 2, "supported": Judge.calls == 1, "passage": None},
            ]

    result = asyncio.run(
        score_record(
            QUESTION,
            record("Recovery 87,5 % (CR-AR-PE-01-15)"),
            Judge(),
            3,  # type: ignore[arg-type]
        )
    )
    assert result["anchor_recall"] == 1.0
    assert result["fact_coverage"] == 1.0
    assert result["judge_recall"] == 0.5
    assert result["unsupported"] == [2]
    assert Judge.calls == 3


def test_judge_failure_leaves_the_question_unmeasured_without_aborting() -> None:
    class Throttled:
        async def judge(self, question: GoldenQuestion, hits: Any) -> list[dict[str, Any]]:
            raise RuntimeError("429 after retries")

    result = asyncio.run(
        score_record(QUESTION, record("Recovery 87,5 %"), Throttled(), 3)  # type: ignore[arg-type]
    )
    assert result["judge_recall"] is None
    assert result["judge_error"] == "RuntimeError"
    assert result["anchor_recall"] == 0.5  # anchors are still measured deterministically
    summary = summarize([{**record("x"), **result}], [])
    assert summary["arms"]["P16"]["judge_errors"] == 1


def test_paired_bootstrap_and_summary() -> None:
    low, high = bootstrap([0.1] * 20)[1:]
    assert low == high == 0.1
    scores = [
        {
            **record("x", arm=arm),
            "id": f"Q{n}",
            "anchor_recall": 1.0,
            "fact_coverage": 1.0,
            "judge_recall": value,
            "judge_agreement": 1.0,
            "passages": 1,
        }
        for n in range(10)
        for arm, value in (("P16", 0.5), ("CEIL", 0.7))
    ]
    summary = summarize(scores, ["P16"])
    paired = summary["paired"]["CEIL-vs-P16"]
    assert paired["n"] == 10
    assert paired["delta"] == 0.2
    assert paired["ci95_low"] == 0.2
    assert summary["arms"]["CEIL"]["categories"]["ALL"]["judge_recall"] == 0.7
