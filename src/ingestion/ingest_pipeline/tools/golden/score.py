"""Score exposed agent contexts with the golden-eval anchors and judge (shared, comparable).

Input: JSONL records from ``python -m crv_agent.evaluation.retrieval`` (one per question and
arm, each with the passages the agent would see). Anchors use the exact golden-eval
``anchor_scores``; the judge is the same ``Judge`` repeated ``--repeats`` times with a
per-fact majority vote. ``truncate`` records are judged exactly like golden-eval (passages cut
at ``PASSAGE_CHARS``); ``split`` records are cut into consecutive passages so the judge sees
all exposed text. Paired bootstrap confidence intervals compare every arm with references.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .dataset import GoldenQuestion
from .judge import PASSAGE_CHARS, Judge
from .retrieval import Hit

CONTEXTS_BEGIN = "=== AGENT CONTEXTS BEGIN ==="
CONTEXTS_END = "=== AGENT CONTEXTS END ==="
SCORE_BEGIN = "=== AGENT SCORE BEGIN ==="
SCORE_END = "=== AGENT SCORE END ==="


def read_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    """JSONL records; tolerates surrounding log lines and the contexts markers."""

    records = []
    inside = None
    for line in lines:
        text = line.strip()
        if text == CONTEXTS_BEGIN:
            inside = True
            continue
        if text == CONTEXTS_END:
            inside = False
            continue
        if inside is False or not text.startswith("{"):
            continue
        records.append(json.loads(text))
    return records


def load_contexts(paths: Sequence[str]) -> list[dict[str, Any]]:
    """Records of every file (``-`` is stdin), e.g. one per search backend scored together."""

    records: list[dict[str, Any]] = []
    for path in paths:
        source = sys.stdin if path == "-" else open(path, encoding="utf-8")  # noqa: SIM115
        try:
            records.extend(read_records(source))
        finally:
            if source is not sys.stdin:
                source.close()
    seen: set[tuple[str, str]] = set()
    for item in records:
        key = (item["id"], item["arm"])
        if key in seen:
            raise SystemExit(f"Duplicate contexts for {key[0]} arm {key[1]}: label each run")
        seen.add(key)
    return records


def _hit(context: dict[str, Any], key: str, text: str) -> Hit:
    return Hit(
        chunk_id=key,
        content_hash=str(context.get("content_hash") or ""),
        title=str(context.get("title") or ""),
        source_path=str(context.get("source") or ""),
        content=text,
        pages=tuple(int(p) for p in context.get("pages") or ()),
        document_code=None,
    )


def split_text(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text) and (space := text.rfind(" ", start + size // 2, end)) > start:
            end = space
        pieces.append(text[start:end])
        start = end
    return pieces or [""]


def hits_of(record: dict[str, Any]) -> tuple[list[Hit], list[Hit]]:
    """(full-text hits for anchors, passages for the judge)."""

    full = [_hit(c, str(c["id"]), str(c["text"])) for c in record["contexts"]]
    if record.get("passage_mode") == "truncate":
        return full, full
    passages = [
        _hit(c, f"{c['id']}~{n}", piece)
        for c in record["contexts"]
        for n, piece in enumerate(split_text(str(c["text"])))
    ]
    return full, passages


def majority(runs: Sequence[Sequence[dict[str, Any]]]) -> tuple[list[bool], float | None]:
    if not runs or not runs[0]:
        return [], None
    votes = [[run[i]["supported"] for run in runs] for i in range(len(runs[0]))]
    supported = [sum(v) * 2 > len(v) for v in votes]
    agreement = sum(len(set(v)) == 1 for v in votes) / len(votes)
    return supported, round(agreement, 3)


async def score_record(
    question: GoldenQuestion,
    record: dict[str, Any],
    judge: Judge | None,
    repeats: int,
) -> dict[str, Any]:
    from .__main__ import anchor_scores

    full, passages = hits_of(record)
    anchors = anchor_scores(question, full, max(1, len(full)))
    result: dict[str, Any] = {
        key: record.get(key)
        for key in (
            "id",
            "category",
            "split",
            "arm",
            "error",
            "seconds",
            "usage",
            "exposed_characters",
            "documents",
            "secondary_share",
            "dossier_cu_share",
        )
    }
    result.update(
        anchor_recall=anchors["anchor_recall"],
        fact_coverage=anchors["fact_coverage"],
        passages=len(passages),
        judge_recall=None,
        judge_agreement=None,
    )
    if judge is not None and not question.must_abstain and not record.get("error"):
        try:
            runs = [await judge.judge(question, passages) for _ in range(repeats)]
        except Exception as error:  # e.g. sustained throttling: unmeasured, never a zero
            result["judge_error"] = type(error).__name__
            return result
        supported, agreement = majority(runs)
        if supported:
            result["judge_recall"] = round(sum(supported) / len(supported), 3)
            result["judge_agreement"] = agreement
            result["unsupported"] = [n + 1 for n, ok in enumerate(supported) if not ok]
    return result


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 3) if present else None


def bootstrap(
    deltas: Sequence[float],
    *,
    resamples: int = 10_000,
    seed: int = 20261021,
) -> tuple[float, float, float] | None:
    if not deltas:
        return None
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(statistics.fmean(rng.choices(deltas, k=n)) for _ in range(resamples))
    return (
        round(statistics.fmean(deltas), 3),
        round(means[int(0.025 * resamples)], 3),
        round(means[int(0.975 * resamples) - 1], 3),
    )


def summarize(
    scores: Sequence[dict[str, Any]],
    references: Sequence[str],
    metric: str = "judge_recall",
) -> dict[str, Any]:
    arms = list(dict.fromkeys(s["arm"] for s in scores))
    by_arm: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in scores:
        by_arm[item["arm"]][item["id"]] = item
    summary: dict[str, Any] = {"arms": {}, "paired": {}}
    for arm in arms:
        items = list(by_arm[arm].values())
        categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            categories[item["category"]].append(item)
            categories["ALL"].append(item)
        seconds = sorted(i["seconds"] for i in items if i.get("seconds") is not None)
        summary["arms"][arm] = {
            "errors": sum(1 for i in items if i.get("error")),
            "judge_errors": sum(1 for i in items if i.get("judge_error")),
            "categories": {
                category: {
                    "n": len(group),
                    "anchor_recall": _mean(i["anchor_recall"] for i in group),
                    "fact_coverage": _mean(i["fact_coverage"] for i in group),
                    "judge_recall": _mean(i["judge_recall"] for i in group),
                    "judge_agreement": _mean(i["judge_agreement"] for i in group),
                }
                for category, group in sorted(categories.items())
            },
            "exposed_characters": _mean(i["exposed_characters"] for i in items),
            "documents": _mean(i["documents"] for i in items),
            "secondary_share": _mean(i["secondary_share"] for i in items),
            "dossier_cu_share": _mean(i["dossier_cu_share"] for i in items),
            "model_calls": _mean((i.get("usage") or {}).get("model_calls") for i in items),
            "input_tokens": _mean((i.get("usage") or {}).get("input_tokens") for i in items),
            "output_tokens": _mean((i.get("usage") or {}).get("output_tokens") for i in items),
            "search_calls": _mean((i.get("usage") or {}).get("retrieval_calls") for i in items),
            "latency_p50": seconds[len(seconds) // 2] if seconds else None,
            "latency_p95": seconds[max(0, int(len(seconds) * 0.95) - 1)] if seconds else None,
        }
    for reference in references:
        if reference not in by_arm:
            continue
        for arm in arms:
            if arm == reference:
                continue
            pairs = [
                (item[metric], by_arm[reference][qid][metric])
                for qid, item in by_arm[arm].items()
                if qid in by_arm[reference]
                and item[metric] is not None
                and by_arm[reference][qid][metric] is not None
            ]
            ci = bootstrap([a - b for a, b in pairs])
            summary["paired"][f"{arm}-vs-{reference}"] = {
                "n": len(pairs),
                "metric": metric,
                **({"delta": ci[0], "ci95_low": ci[1], "ci95_high": ci[2]} if ci else {}),
            }
    return summary


def table(summary: dict[str, Any]) -> str:
    lines = ["arm        n   anchor  facts   judge   agree   chars    docs  p50s"]
    for arm, data in summary["arms"].items():
        row = data["categories"].get("ALL", {})
        lines.append(
            f"{arm:<10} {row.get('n', 0):<3} {row.get('anchor_recall')!s:<7} "
            f"{row.get('fact_coverage')!s:<7} {row.get('judge_recall')!s:<7} "
            f"{row.get('judge_agreement')!s:<7} {data['exposed_characters']!s:<8} "
            f"{data['documents']!s:<5} {data['latency_p50']!s}"
        )
    for name, item in summary["paired"].items():
        lines.append(
            f"{name:<22} n={item['n']:<3} delta={item.get('delta')} "
            f"ci95=[{item.get('ci95_low')}, {item.get('ci95_high')}]"
        )
    return "\n".join(lines)


async def run_score(args: argparse.Namespace) -> int:
    from .__main__ import load_golden

    questions = {q.id: q for q in load_golden(args.golden)}
    records = load_contexts(args.contexts)
    unknown = sorted({r["id"] for r in records} - questions.keys())
    if unknown:
        raise SystemExit(f"Unknown question ids in contexts: {unknown[:5]}")
    judge = None
    credential = None
    if args.judge_deployment:
        from azure.identity.aio import DefaultAzureCredential

        from ...adapters.azure_ai.embeddings import create_openai_client

        credential = DefaultAzureCredential()
        judge = Judge(
            create_openai_client(args.openai_endpoint, credential, api_version=args.api_version),
            deployment=args.judge_deployment,
            concurrency=args.concurrency,
        )
    try:
        gate = asyncio.Semaphore(args.concurrency)

        async def one(record: dict[str, Any]) -> dict[str, Any]:
            async with gate:
                return await score_record(questions[record["id"]], record, judge, args.repeats)

        scores = await asyncio.gather(*(one(r) for r in records))
    finally:
        if credential is not None:
            await credential.close()
    summary = summarize(scores, args.reference or ["P16", "A1"])
    report = {
        "judge": {
            "deployment": args.judge_deployment,
            "repeats": args.repeats,
            "passage_chars": PASSAGE_CHARS,
        },
        "summary": summary,
        "scores": scores,
    }
    print(table(summary))
    print(SCORE_BEGIN)
    print(json.dumps(report, ensure_ascii=False))
    print(SCORE_END)
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return 0


def add_parser(commands: Any) -> None:
    score = commands.add_parser(
        "score",
        help="Score agent contexts (JSONL) with golden-eval anchors and judge",
    )
    score.add_argument("--golden", required=True)
    score.add_argument("--contexts", required=True, action="append",
                       help="JSONL path (repeatable), or - for stdin")
    score.add_argument("--judge-deployment", default="")
    score.add_argument("--openai-endpoint", default="")
    score.add_argument("--api-version", default="2025-04-01-preview")
    score.add_argument("--repeats", type=int, default=3)
    score.add_argument("--reference", action="append", help="Arm for paired deltas")
    score.add_argument("--concurrency", type=int, default=6)
    score.add_argument("--out", default="")
