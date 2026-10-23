"""Golden-set retrieval evaluation command line.

``convert`` (local): Docs/QA_REMINEX.xlsx Sheet2 -> golden/crv_golden.jsonl.
``run`` (cluster): retrieve per question and mode, score anchors and the LLM judge, print the
report between markers (the index and the knowledge graph are private).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .dataset import AnchorText, GoldenQuestion, anchor_hits, build_questions
from .judge import Judge
from .retrieval import MODES, Hit, KgReader, Retriever

REPORT_BEGIN = "=== GOLDEN REPORT BEGIN ==="
REPORT_END = "=== GOLDEN REPORT END ==="
DEFAULT_SCOPES = ("crv:research", "crv:geometallurgy", "crv:environment")


def _convert(args: argparse.Namespace) -> int:
    import openpyxl  # type: ignore[import-untyped]

    book = openpyxl.load_workbook(args.xlsx, read_only=True, data_only=True)
    rows = [
        (str(row[0]), str(row[1] or ""))
        for row in list(book[args.sheet].iter_rows(values_only=True))[1:]
        if row and row[0]
    ]
    questions = build_questions(rows)
    Path(args.out).write_text(
        "".join(q.model_dump_json() + "\n" for q in questions), encoding="utf-8"
    )
    anchors = sum(len(f.anchors) for q in questions for f in q.key_facts)
    facts = sum(len(q.key_facts) for q in questions)
    print(json.dumps({"questions": len(questions), "facts": facts, "anchors": anchors,
                      "facts_without_anchors": sum(1 for q in questions for f in q.key_facts
                                                   if not f.anchors)}))
    return 0


def load_golden(path: str) -> list[GoldenQuestion]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [GoldenQuestion.model_validate_json(line) for line in lines if line.strip()]


def anchor_scores(question: GoldenQuestion, hits: Sequence[Hit], k: int) -> dict[str, Any]:
    texts = [AnchorText(f"{hit.title}\n{hit.content}") for hit in hits[:k]]
    anchors = [a for fact in question.key_facts for a in fact.anchors]
    found = anchor_hits(list(dict.fromkeys(anchors)), texts)
    with_anchors = [fact for fact in question.key_facts if fact.anchors]
    covered = [fact for fact in with_anchors if set(fact.anchors) <= found]
    return {
        "anchor_recall": round(len(found) / len(set(anchors)), 3) if anchors else None,
        "fact_coverage": round(len(covered) / len(with_anchors), 3) if with_anchors else None,
        "missing": sorted(set(anchors) - found)[:12],
    }


def _documents(hits: Sequence[Hit], limit: int = 5) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(hits, start=1):
        entry = seen.setdefault(hit.content_hash, {
            "source_path": hit.source_path, "first_rank": rank, "chunks": 0,
            "document_code": hit.document_code,
        })
        entry["chunks"] += 1
    return list(seen.values())[:limit]


async def _evaluate(
    question: GoldenQuestion,
    retriever: Retriever,
    judge: Judge | None,
    *,
    modes: Sequence[str],
    ks: Sequence[int],
    judge_k: int,
    gate: asyncio.Semaphore,
) -> dict[str, Any]:
    result: dict[str, Any] = {"id": question.id, "category": question.category,
                              "split": question.split, "modes": {}}
    async with gate:
        for mode in modes:
            retrieved = await retriever.retrieve(question.question, mode, max(ks))
            hits = retrieved.hits
            entry: dict[str, Any] = {
                "seconds": round(retrieved.seconds, 3),
                "anchors": {str(k): anchor_scores(question, hits, k) for k in ks},
                "documents": _documents(hits[:judge_k]),
            }
            if mode.startswith("kg"):
                entry["kg_entities"] = [
                    {key: item[key] for key in ("iri", "kind", "status")}
                    for item in retrieved.kg_entities
                ]
                entry["kg_documents"] = retrieved.kg_documents
            if judge is not None and not question.must_abstain:
                verdicts = await judge.judge(question, hits[:judge_k])
                supported = [v for v in verdicts if v["supported"]]
                cited = {v["passage"] for v in supported if v["passage"]}
                entry["judge"] = {
                    "supported": len(supported),
                    "facts": len(verdicts),
                    "recall": round(len(supported) / len(verdicts), 3) if verdicts else None,
                    "sources": sorted({hit.source_path for hit in hits if hit.chunk_id in cited}),
                    "unsupported": [v["fact"] for v in verdicts if not v["supported"]],
                }
            result["modes"][mode] = entry
    best = max(
        result["modes"].items(),
        key=lambda item: (item[1].get("judge", {}).get("recall") or 0,
                          item[1]["anchors"][str(judge_k)]["anchor_recall"] or 0),
    )
    result["proposed_sources"] = best[1].get("judge", {}).get("sources") or [
        doc["source_path"] for doc in best[1]["documents"][:3]
    ]
    return result


def _mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(statistics.fmean(present), 3) if present else None


def summarize(results: Sequence[dict[str, Any]], modes: Sequence[str], ks: Sequence[int],
              judge_k: int) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in modes:
        per_category: dict[str, dict[str, Any]] = {}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for result in results:
            groups[result["category"]].append(result)
            groups["ALL"].append(result)
        for category, items in sorted(groups.items()):
            entries = [item["modes"][mode] for item in items]
            row: dict[str, Any] = {"questions": len(items)}
            for k in ks:
                row[f"anchor_recall@{k}"] = _mean([e["anchors"][str(k)]["anchor_recall"]
                                                   for e in entries])
                row[f"fact_coverage@{k}"] = _mean([e["anchors"][str(k)]["fact_coverage"]
                                                   for e in entries])
            row[f"judge_recall@{judge_k}"] = _mean([e.get("judge", {}).get("recall")
                                                     for e in entries])
            per_category[category] = row
        seconds = sorted(item["modes"][mode]["seconds"] for item in results)
        summary[mode] = {
            "categories": per_category,
            "latency_p50": seconds[len(seconds) // 2] if seconds else None,
            "latency_p95": seconds[int(len(seconds) * 0.95) - 1] if seconds else None,
        }
    return summary


def _table(summary: dict[str, Any], judge_k: int, k: int) -> str:
    lines = [f"mode      cat  n   anchor@{k}  facts@{k}  judge@{judge_k}"]
    for mode, data in summary.items():
        for category, row in data["categories"].items():
            lines.append(
                f"{mode:<9} {category:<4} {row['questions']:<3} "
                f"{row.get(f'anchor_recall@{k}')!s:<10} {row.get(f'fact_coverage@{k}')!s:<9} "
                f"{row.get(f'judge_recall@{judge_k}')!s}"
            )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    from azure.identity.aio import DefaultAzureCredential
    from azure.search.documents.aio import SearchClient

    from ...adapters.azure_ai.embeddings import create_openai_client
    from ...adapters.postgres.pool import create_pool
    from ...config import PipelineSettings, require

    settings = PipelineSettings()
    questions = load_golden(args.golden)
    if args.only:
        questions = [q for q in questions if q.id in set(args.only)]
    modes = [mode for mode in args.mode or MODES if mode in MODES]
    ks = sorted({int(k) for k in args.k.split(",")})
    async with DefaultAzureCredential() as credential:
        needs_kg = any(mode.startswith("kg") for mode in modes)
        pool = await create_pool(settings.db, credential) if needs_kg else None
        try:
            kg = None
            if pool is not None:
                kg = KgReader(pool, environment=args.kg_environment, scopes=args.scope
                              or DEFAULT_SCOPES)
                await kg.open()
            async with SearchClient(
                require(settings.search.endpoint, "TIZ_SEARCH__ENDPOINT"),
                settings.search.index_name, credential,
            ) as search:
                retriever = Retriever(search, release_id=args.release, kg=kg)
                judge = None
                if args.judge_deployment:
                    client = create_openai_client(
                        require(settings.embeddings.endpoint, "TIZ_EMBEDDINGS__ENDPOINT"),
                        credential, api_version=args.api_version,
                    )
                    judge = Judge(client, deployment=args.judge_deployment,
                                  concurrency=args.concurrency)
                gate = asyncio.Semaphore(args.concurrency)
                results = await asyncio.gather(*(
                    _evaluate(q, retriever, judge, modes=modes, ks=ks, judge_k=args.judge_k,
                              gate=gate)
                    for q in questions
                ))
        finally:
            if pool is not None:
                await pool.close()
    report = {
        "release": args.release,
        "kg_release": kg.release_id if kg else None,
        "modes": modes,
        "ks": ks,
        "judge": {"deployment": args.judge_deployment, "k": args.judge_k},
        "summary": summarize(results, modes, ks, args.judge_k),
        "questions": results,
    }
    print(_table(report["summary"], args.judge_k, ks[0]))
    print(REPORT_BEGIN)
    print(json.dumps(report, ensure_ascii=False))
    print(REPORT_END)
    return 0


def _sources_sheet(args: argparse.Namespace) -> int:
    """Expert review sheet: proposed sources per question (judge-cited, never ground truth)."""

    import csv
    import io

    questions = {q.id: q for q in load_golden(args.golden)}
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    buffer = io.StringIO()
    fields = ["id", "category", "split", "question", "expected_answer", "proposed_sources",
              "status", "confirmed_sources", "expert_comment"]
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    for result in report["questions"]:
        question = questions[result["id"]]
        writer.writerow({
            "id": question.id,
            "category": question.category,
            "split": question.split,
            "question": question.question,
            "expected_answer": question.expected,
            "proposed_sources": "\n".join(result.get("proposed_sources") or []),
            "status": "proposed" if not question.must_abstain else "n/a (abstention)",
            "confirmed_sources": "",
            "expert_comment": "",
        })
    Path(args.out).write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
    print(json.dumps({"rows": len(report["questions"]), "out": args.out}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest_pipeline.tools.golden")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="Sheet2 of the golden workbook to JSONL")
    convert.add_argument("--xlsx", required=True)
    convert.add_argument("--sheet", default="Sheet2")
    convert.add_argument("--out", required=True)
    sheet = commands.add_parser(
        "sources-sheet", help="Expert review sheet of proposed expected sources (offline)"
    )
    sheet.add_argument("--golden", required=True)
    sheet.add_argument("--report", required=True, help="JSON report of a run")
    sheet.add_argument("--out", required=True)
    run = commands.add_parser("run", help="Evaluate retrieval in the cluster")
    run.add_argument("--golden", required=True)
    run.add_argument("--release", default="crv-v1")
    run.add_argument("--mode", action="append", choices=MODES)
    run.add_argument("--k", default="10,20,50")
    run.add_argument("--judge-k", type=int, default=10)
    run.add_argument("--judge-deployment", default="")
    run.add_argument("--api-version", default="2025-04-01-preview")
    run.add_argument("--kg-environment", default="dev")
    run.add_argument("--scope", action="append")
    run.add_argument("--only", action="append", help="Question id (repeatable)")
    run.add_argument("--concurrency", type=int, default=6)
    from .audit import add_parser as add_audit_parser
    from .audit import run_audit
    from .score import add_parser as add_score_parser
    from .score import run_score
    from .status_review import add_parser as add_review_parser
    from .status_review import run_status_review

    add_score_parser(commands)
    add_audit_parser(commands)
    add_review_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "convert":
        return _convert(args)
    if args.command == "sources-sheet":
        return _sources_sheet(args)
    if args.command in {"score", "audit", "status-review"}:
        if args.judge_deployment and not args.openai_endpoint:
            parser.error(f"{args.command} --judge-deployment needs --openai-endpoint")
        runner = {"score": run_score, "audit": run_audit, "status-review": run_status_review}
        return asyncio.run(runner[args.command](args))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
