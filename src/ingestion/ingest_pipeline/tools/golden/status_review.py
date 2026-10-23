"""Status review sheet: answers labelled ``insufficient_evidence`` that still carry claims.

Selects, from one or more answer-eval ``answers.jsonl`` files, every ``insufficient_evidence``
answer with at least one verified claim, every ``must_abstain`` question, and a few ``partial``
answers as controls. An optional LLM pre-label (answered / partly / not_answered, plus whether
the question's premise is false) is written next to blank expert columns in a CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...adapters.azure_ai.structured import structured
from .dataset import GoldenQuestion

ANSWER_CHARS = 3000
_INSTRUCTIONS = (
    "You review a research assistant's answer. Given the QUESTION, the reference KEY FACTS and "
    "the ANSWER, label whether the answer addresses the question's main ask: 'answered' (the "
    "main fact, value or document is stated), 'partly' (related, useful evidence but the main "
    "ask is not established), or 'not_answered'. Set premise_false when the question assumes "
    "something the key facts contradict. Give a one-sentence rationale."
)
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "premise_false", "rationale"],
    "properties": {
        "label": {"type": "string", "enum": ["answered", "partly", "not_answered"]},
        "premise_false": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}
COLUMNS = (
    "id",
    "arm",
    "category",
    "must_abstain",
    "selection",
    "status",
    "judge_recall",
    "claims",
    "question",
    "key_facts",
    "answer",
    "llm_label",
    "llm_premise_false",
    "llm_rationale",
    "expert_label",
    "expert_note",
)


def select(
    questions: dict[str, GoldenQuestion],
    rows: Sequence[dict[str, Any]],
    controls: int,
) -> list[tuple[str, dict[str, Any]]]:
    """(selection reason, row): insufficient with claims, must-abstain, then partial controls."""

    chosen: list[tuple[str, dict[str, Any]]] = []
    partial: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (str(r.get("arm")), int(str(r["id"]).lstrip("Q")))):
        claims = (row.get("answer") or {}).get("claims") or []
        if questions[row["id"]].must_abstain:
            chosen.append(("must_abstain", row))
        elif row.get("status") == "insufficient_evidence" and claims:
            chosen.append(("insufficient_with_claims", row))
        elif row.get("status") == "partial" and claims:
            partial.append(row)
    return chosen + [("partial_control", row) for row in partial[:controls]]


async def label(
    client: Any, deployment: str, question: GoldenQuestion, answer: str
) -> dict[str, Any]:
    facts = "\n".join(f"- {fact.text}" for fact in question.key_facts)
    return await structured(
        client,
        deployment,
        _INSTRUCTIONS,
        f"QUESTION: {question.question}\n\nKEY FACTS:\n{facts}\n\nANSWER:\n{answer[:ANSWER_CHARS]}",
        "status_review",
        _SCHEMA,
    )


async def build_rows(
    questions: dict[str, GoldenQuestion],
    rows: Sequence[dict[str, Any]],
    recall: dict[tuple[str, str], float | None],
    controls: int,
    client: Any = None,
    deployment: str = "",
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    gate = asyncio.Semaphore(concurrency)

    async def one(reason: str, row: dict[str, Any]) -> dict[str, Any]:
        question = questions[row["id"]]
        answer = row.get("answer") or {}
        content = str(answer.get("content") or "")
        verdict: dict[str, Any] = {}
        if client is not None:
            async with gate:
                verdict = await label(client, deployment, question, content)
        return {
            "id": row["id"],
            "arm": row.get("arm"),
            "category": question.category,
            "must_abstain": question.must_abstain,
            "selection": reason,
            "status": row.get("status"),
            "judge_recall": recall.get((row["id"], str(row.get("arm")))),
            "claims": len(answer.get("claims") or []),
            "question": question.question,
            "key_facts": " | ".join(fact.text for fact in question.key_facts),
            "answer": content[:ANSWER_CHARS],
            "llm_label": verdict.get("label", ""),
            "llm_premise_false": verdict.get("premise_false", ""),
            "llm_rationale": verdict.get("rationale", ""),
            "expert_label": "",
            "expert_note": "",
        }

    return list(await asyncio.gather(*(one(*item) for item in select(questions, rows, controls))))


async def run_status_review(args: argparse.Namespace) -> int:
    from .__main__ import load_golden
    from .audit import read_jsonl

    questions = {q.id: q for q in load_golden(args.golden)}
    rows = [row for path in args.answers for row in read_jsonl(path)]
    recall: dict[tuple[str, str], float | None] = {}
    for path in args.score or []:
        for item in json.loads(Path(path).read_text(encoding="utf-8-sig"))["scores"]:
            recall[(item["id"], item["arm"])] = item.get("judge_recall")
    client, credential = None, None
    if args.judge_deployment:
        from azure.identity.aio import DefaultAzureCredential

        from ...adapters.azure_ai.embeddings import create_openai_client

        credential = DefaultAzureCredential()
        client = create_openai_client(
            args.openai_endpoint, credential, api_version=args.api_version
        )
    try:
        sheet = await build_rows(
            questions, rows, recall, args.controls, client, args.judge_deployment, args.concurrency
        )
    finally:
        if credential is not None:
            await credential.close()
    with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sheet)
    labels: dict[str, int] = {}
    for row in sheet:
        key = f"{row['selection']}:{row['llm_label'] or 'unlabelled'}"
        labels[key] = labels.get(key, 0) + 1
    print(json.dumps({"rows": len(sheet), "labels": labels, "out": args.out}))
    return 0


def add_parser(commands: Any) -> None:
    review = commands.add_parser(
        "status-review",
        help="CSV of insufficient-evidence answers with claims for review",
    )
    review.add_argument("--golden", required=True)
    review.add_argument("--answers", action="append", required=True, help="answers.jsonl")
    review.add_argument("--score", action="append", help="score.json for judge recall")
    review.add_argument("--controls", type=int, default=5)
    review.add_argument("--judge-deployment", default="", help="Optional LLM pre-label")
    review.add_argument("--openai-endpoint", default="")
    review.add_argument("--api-version", default="2025-04-01-preview")
    review.add_argument("--concurrency", type=int, default=4)
    review.add_argument("--out", required=True)
