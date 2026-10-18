"""Summarize an agent benchmark run and assign cases to a stable dev / held-out split.

Cases sharing a source document (or photo) stay in the same split, and every dataset
contributes to both halves, so tuning on dev cannot leak into the held-out score.

    python analyze_benchmark.py <results-*.json> [--split-out split.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def group_key(question: dict[str, Any]) -> str:
    sources = (
        question.get("source_documents") or question.get("photos") or [question["question_id"]]
    )
    return "|".join(sorted(str(source) for source in sources))


def split_cases(questions: list[dict[str, Any]]) -> dict[str, str]:
    """``question_id -> dev|heldout``: groups alternate per dataset in hash order."""

    by_dataset: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for question in questions:
        dataset = by_dataset[question.get("dataset") or ""]
        dataset[group_key(question)].append(question["question_id"])
    assignment = {}
    for groups in by_dataset.values():
        ordered = sorted(groups, key=lambda key: hashlib.sha256(key.encode("utf-8")).hexdigest())
        for index, key in enumerate(ordered):
            for question_id in groups[key]:
                assignment[question_id] = "dev" if index % 2 == 0 else "heldout"
    return assignment


def case_row(result: dict[str, Any], split: str) -> dict[str, Any]:
    question = result["question"]
    done = result.get("done") or {}
    evaluation = result.get("answer_evaluation") or {}
    return {
        "id": question["question_id"],
        "split": split,
        "dataset": question.get("dataset"),
        "modality": question.get("input_modality"),
        "behavior": question.get("expected_behavior"),
        "coverage": (result.get("source_coverage") or {}).get("status"),
        "score": evaluation.get("score"),
        "source_matched": (result.get("retrieval_source_review") or {}).get("matched"),
        "first_token_s": result.get("first_token_seconds"),
        "elapsed_s": result.get("agent_elapsed_seconds"),
        "clarified": done.get("needs_clarification"),
        "citations": len(done.get("citations") or []),
        "reason": (evaluation.get("reason") or result.get("error") or "")[:240],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--split-out", type=Path)
    args = parser.parse_args()

    data = json.loads(args.results.read_text(encoding="utf-8"))
    results = data["results"]
    split = split_cases([result["question"] for result in results])
    rows = [case_row(result, split[result["question"]["question_id"]]) for result in results]
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    for field in ("dataset", "split", "modality"):
        groups: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            groups[str(row[field])].append(row["score"] or 0.0)
        print(
            json.dumps(
                {
                    f"by_{field}": {
                        key: {
                            "cases": len(scores),
                            "full": sum(score == 1.0 for score in scores),
                            "mean": round(sum(scores) / len(scores), 3),
                        }
                        for key, scores in sorted(groups.items())
                    }
                },
                ensure_ascii=False,
            )
        )
    if args.split_out:
        args.split_out.write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
