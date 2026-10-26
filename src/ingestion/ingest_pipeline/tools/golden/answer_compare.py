"""Offline stakeholder-reference comparison, execution metrics and resumable local reports."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .business import BusinessQuestion, ToolRule
from .dataset import GoldenQuestion

RUBRIC_VERSION = "stakeholder-answer-v3"


class FactComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    index: int
    result: Literal["matched", "partial", "missing", "contradicted"]
    expected_excerpt: str
    generated_excerpt: str | None
    explanation: str


class ComparisonVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["pass", "partial", "fail", "needs_review"]
    expected_behavior_met: bool
    reference_ambiguous: bool
    facts: list[FactComparison]
    issues: list[str]
    rationale: str


class AnswerJudge(Protocol):
    async def compare(
        self,
        question: GoldenQuestion,
        answer: str,
    ) -> tuple[ComparisonVerdict, dict[str, Any]]: ...


def validate_verdict(
    question: GoldenQuestion,
    answer: str,
    verdict: ComparisonVerdict,
) -> None:
    indices = [fact.index for fact in verdict.facts]
    if sorted(indices) != list(range(1, len(question.key_facts) + 1)):
        raise ValueError("Judge must evaluate each frozen reference element exactly once")
    for fact in verdict.facts:
        expected = question.key_facts[fact.index - 1].text
        if not fact.expected_excerpt or fact.expected_excerpt not in expected:
            raise ValueError("Judge reference excerpt is not in its frozen reference element")
        if fact.generated_excerpt is not None and (
            not fact.generated_excerpt or fact.generated_excerpt not in answer
        ):
            raise ValueError("Judge generated excerpt is not in the actual answer")
        if fact.result in {"matched", "partial", "contradicted"} and not fact.generated_excerpt:
            raise ValueError("An observed match or contradiction requires a generated excerpt")
    if verdict.verdict == "pass" and (
        not verdict.expected_behavior_met
        or verdict.reference_ambiguous
        or any(fact.result != "matched" for fact in verdict.facts)
    ):
        raise ValueError("Passing verdict contradicts its fact or behavior checks")
    if verdict.reference_ambiguous and verdict.verdict != "needs_review":
        raise ValueError("An ambiguous business reference requires review")


def tool_metrics(record: dict[str, Any], rule: ToolRule) -> dict[str, Any]:
    calls = record.get("tool_invocations")
    complete = record.get("tool_trace_status") == "complete" and isinstance(calls, list)
    if calls is not None and not isinstance(calls, list):
        raise ValueError("tool_invocations must be a list")
    distinct: dict[str, dict[str, Any]] = {}
    for call in calls or []:
        if not isinstance(call.get("id"), str) or not isinstance(call.get("name"), str):
            raise ValueError("Observed tool invocations require string id and name")
        if call["id"] in distinct and distinct[call["id"]] != call:
            raise ValueError("Conflicting observations for one tool invocation")
        distinct[call["id"]] = call
    names = Counter(call["name"] for call in distinct.values())
    applicable = bool(rule.all_of or rule.any_of or rule.forbidden)
    observed_violation = bool(set(rule.forbidden) & names.keys())
    required_seen = set(rule.all_of) <= names.keys() and (
        not rule.any_of or bool(set(rule.any_of) & names.keys())
    )
    result: bool | None = None
    status = "not_applicable" if not applicable else "unmeasured"
    if applicable and (complete or observed_violation):
        result, status = required_seen and not observed_violation, "measured"
    return {
        "expected_tool_called": result,
        "tool_check_status": status,
        "tool_call_count": len(distinct) if complete and calls is not None else None,
        "tool_counts": dict(names),
        "tool_error_count": sum(call.get("outcome") == "error" for call in distinct.values())
        if complete and calls is not None
        else None,
        "tool_trace_status": "complete" if complete and calls is not None else "unmeasured",
    }


def usage_metrics(record: dict[str, Any]) -> dict[str, Any]:
    usage = record.get("usage") or {}

    def valid(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    measured = valid(inputs) and valid(outputs)
    result = {
        key: usage.get(key) if valid(usage.get(key)) else None
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "model_calls",
            "retrieval_calls",
            "reserved_tokens",
        )
    }
    return {
        **result,
        "total_tokens": inputs + outputs
        if isinstance(inputs, int) and isinstance(outputs, int) and measured
        else None,
        "token_usage_status": "measured" if measured else "unmeasured",
        "cached_input_tokens": result["cached_tokens"],
        "agent_cost_estimated": None,
        "agent_cost_actual": None,
        "agent_cost_status": "unmeasured",
        "judge_cost_estimated": None,
        "judge_cost_actual": None,
        "evaluation_total_cost": None,
    }


def estimated_cost(record: dict[str, Any], price_book: dict[str, Any] | None) -> dict[str, Any]:
    if price_book is None or not record.get("model_usage"):
        return {}
    if not all(
        price_book.get(key) for key in ("currency", "effective_date", "source_url", "models")
    ):
        raise ValueError("A price book needs currency, effective_date, source_url and models")
    total, missing = Decimal(0), []
    calls: dict[str, dict[str, Any]] = {}
    for call in record["model_usage"]:
        if not isinstance(call.get("id"), str) or call.get("usage_source") != "provider":
            raise ValueError("Cost estimates require identified provider-reported model usage")
        if call["id"] in calls and calls[call["id"]] != call:
            raise ValueError("Conflicting token observations for one model call")
        calls[call["id"]] = call
    for call in calls.values():
        rates = price_book["models"].get(call["model"])
        if rates is None:
            missing.append(call["model"])
            continue
        inputs, outputs, cached = (
            call.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        )
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (inputs, outputs, cached)
        ):
            raise ValueError("Invalid provider token totals or cache subset")
        inputs, outputs, cached = (
            int(call[key]) for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        )
        if cached > inputs:
            raise ValueError("Invalid provider token totals or cache subset")
        prices = [
            Decimal(str(rates[key]))
            for key in ("input_per_million", "output_per_million", "cached_input_per_million")
        ]
        if any(not price.is_finite() or price < 0 for price in prices):
            raise ValueError("Prices must be finite and nonnegative")
        total += (
            (inputs - cached) * prices[0] + outputs * prices[1] + cached * prices[2]
        ) / 1_000_000
    complete = not missing and record.get("model_usage_status") == "complete"
    return {
        "agent_cost_estimated": str(total) if complete else None,
        "agent_cost_known_subtotal": str(total),
        "agent_cost_status": "estimated" if complete else "partial",
        "cost_currency": price_book["currency"],
        "price_book_date": price_book["effective_date"],
        "price_book_source": price_book["source_url"],
        "unpriced_models": missing,
    }


def comparison_record(
    question: GoldenQuestion,
    record: dict[str, Any],
    attempts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    metadata = question.evaluation if isinstance(question, BusinessQuestion) else None
    tools = tool_metrics(record, metadata.tools if metadata else ToolRule())
    valid = [
        ComparisonVerdict.model_validate(item["comparison"])
        for item in attempts
        if item.get("comparison") is not None
    ]
    errors = len(attempts) - len(valid)
    labels = [item.verdict for item in valid]
    verdict: str = labels[0] if labels and len(set(labels)) == 1 and not errors else "needs_review"
    reasons: list[str] = []
    if not valid:
        verdict = "error" if attempts else "not_evaluated"
    if metadata and metadata.review_notes:
        verdict = "needs_review"
        reasons.extend(metadata.review_notes)
    completed = record.get("execution_status") == "completed" and not record.get("turn_error")
    if not completed:
        verdict = "not_evaluated"
        reasons.append("generation_not_completed")
    if record.get("schema_valid") is not True:
        reasons.append("answer_schema_not_verified")
    if tools["tool_check_status"] != "not_applicable" and tools["expected_tool_called"] is not True:
        reasons.append("mandatory_tool_check_failed_or_unmeasured")
    if verdict != "pass":
        reasons.append(f"comparison_{verdict}")
    if errors:
        reasons.append("judge_attempt_failed")
    answer = record.get("answer") or {}
    facts = [fact for comparison in valid for fact in comparison.facts]
    premiums = [
        event["usage"]["premiumRequests"]
        for attempt in attempts
        for event in (attempt.get("usage") or {}).get("events", [])
        if event.get("type") == "result"
        and isinstance(event.get("usage", {}).get("premiumRequests"), (int, float))
    ]
    return {
        "record_version": "comparison-v1",
        "rubric_version": RUBRIC_VERSION,
        "id": question.id,
        "arm": record.get("arm", "AGENT"),
        "question": question.question,
        "expected_answer": question.expected,
        "generated_answer": answer.get("content", record.get("raw_answer_content") or ""),
        "answer_status": record.get("status"),
        "execution_status": record.get("execution_status", "unmeasured"),
        "comparison_verdict": verdict,
        "comparison_rationale": " | ".join(dict.fromkeys(item.rationale for item in valid)),
        "matched_elements": list(
            dict.fromkeys(fact.expected_excerpt for fact in facts if fact.result == "matched")
        ),
        "missing_elements": list(
            dict.fromkeys(
                fact.expected_excerpt for fact in facts if fact.result in {"missing", "partial"}
            )
        ),
        "contradicted_elements": list(
            dict.fromkeys(fact.expected_excerpt for fact in facts if fact.result == "contradicted")
        ),
        "comparison_issues": list(dict.fromkeys(issue for item in valid for issue in item.issues)),
        "reference_fact_recall": sum(fact.result == "matched" for fact in facts) / len(facts)
        if facts
        else None,
        "task_success": completed and not reasons,
        "failure_or_blocked_reasons": reasons,
        "schema_valid": record.get("schema_valid") is True,
        "schema_check_status": record.get("schema_check_status", "unmeasured"),
        "schema_errors": record.get("schema_errors", []),
        "latency_ms": record.get("latency_ms"),
        "total_execution_ms": record.get("total_execution_ms"),
        "first_finding_ms": record.get("first_finding_ms"),
        "request_id": record.get("request_id"),
        "thread_id": record.get("thread_id"),
        "trace_id": record.get("trace_id"),
        "search_release": record.get("search_release"),
        "kg_release": record.get("kg_release"),
        "usage": record.get("usage", {}),
        "role_usage": record.get("role_usage", {}),
        "citations": answer.get("citations", []),
        "coverage": answer.get("coverage"),
        "expected_behavior": metadata.behavior if metadata else "legacy_reference",
        "reference_metadata": metadata.model_dump(mode="json") if metadata else None,
        **tools,
        **usage_metrics(record),
        **record.get("cost_metrics", {}),
        "judge_attempts": len(attempts),
        "judge_errors": errors,
        "judge_schema_valid": all(item.get("judge_schema_valid") is True for item in attempts)
        if attempts
        else None,
        "judge_premium_requests": float(sum(Decimal(str(value)) for value in premiums))
        if premiums
        else None,
        "judge_agreement": max(Counter(labels).values()) / len(labels) if labels else None,
        "judge_latency_ms": sum(item.get("latency_ms", 0) for item in attempts),
        "judge_usage": [item.get("usage", {}) for item in attempts],
        "comparisons": [item.model_dump(mode="json") for item in valid],
        "groundedness": "unmeasured",
        "human_review_status": "pending",
    }


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


async def compare_records(
    questions: Sequence[GoldenQuestion],
    records: Sequence[dict[str, Any]],
    judge: AnswerJudge | None,
    *,
    repeats: int = 3,
    emit: Callable[[dict[str, Any]], None] | None = None,
    cached: Sequence[dict[str, Any]] = (),
    model: str = "",
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    if repeats not in {1, 3, 5} or not 1 <= concurrency <= 8:
        raise ValueError("Use 1, 3 or 5 repeats and concurrency between 1 and 8")
    by_id = {question.id: question for question in questions}
    if len(by_id) != len(questions):
        raise ValueError("Duplicate golden question IDs")
    keys = [(item["id"], item.get("arm", "AGENT")) for item in records]
    if len(set(keys)) != len(keys) or any(key[0] not in by_id for key in keys):
        raise ValueError("Unknown or duplicate answer IDs within an arm")
    arms = {key[1] for key in keys} or {"AGENT"}
    selected = {(question.id, arm) for question in questions for arm in arms}
    rows = {(item["id"], item.get("arm", "AGENT")): item for item in records}
    cached_by_key = {(item["fingerprint"], item["repeat"]): item for item in cached}
    if len(cached_by_key) != len(cached):
        raise ValueError("Duplicate cached judge attempts")
    gate = asyncio.Semaphore(concurrency)

    async def one(key: tuple[str, str]) -> dict[str, Any]:
        question = by_id[key[0]]
        row = rows.get(key, {"id": key[0], "arm": key[1], "execution_status": "not_run"})
        digest = fingerprint(
            {
                "question": question.model_dump(mode="json"),
                "answer": row,
                "rubric": RUBRIC_VERSION,
                "model": model,
                "repeats": repeats,
            }
        )
        attempts = []
        if (
            judge is not None
            and row.get("execution_status") == "completed"
            and row.get("schema_valid") is True
        ):
            for repeat in range(repeats):
                previous = cached_by_key.get((digest, repeat))
                if previous is not None:
                    attempts.append(previous)
                    continue
                attempt: dict[str, Any] = {
                    "fingerprint": digest,
                    "id": key[0],
                    "arm": key[1],
                    "repeat": repeat,
                    "model": model,
                    "rubric_version": RUBRIC_VERSION,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                started = asyncio.get_running_loop().time()
                try:
                    async with gate:
                        result, usage = await judge.compare(question, row["answer"]["content"])
                    attempt["usage"] = usage
                    attempt["received_comparison"] = result.model_dump(mode="json")
                    validate_verdict(question, row["answer"]["content"], result)
                    attempt.update(
                        comparison=result.model_dump(mode="json"),
                        usage=usage,
                        judge_schema_valid=True,
                    )
                except Exception as error:
                    attempt.update(
                        comparison=None, error=type(error).__name__, judge_schema_valid=False
                    )
                    observed_usage = getattr(error, "usage", None)
                    received_content = getattr(error, "received_content", None)
                    if isinstance(observed_usage, dict):
                        attempt["usage"] = observed_usage
                    if isinstance(received_content, str):
                        attempt["received_content"] = received_content
                    if type(error) is ValueError:
                        attempt["validation_error"] = str(error)
                    elif isinstance(getattr(error, "validation_error", None), str):
                        attempt["validation_error"] = getattr(error, "validation_error")
                attempt["latency_ms"] = 1000 * (asyncio.get_running_loop().time() - started)
                attempts.append(attempt)
                if emit is not None:
                    emit(attempt)
        return comparison_record(question, row, attempts)

    return list(await asyncio.gather(*(one(key) for key in sorted(selected))))


def spreadsheet_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def write_report(directory: Path, comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "comparisons.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in comparisons),
        encoding="utf-8",
    )
    fields = list(dict.fromkeys(key for item in comparisons for key in item))
    with (directory / "comparisons.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {key: spreadsheet_cell(value) for key, value in item.items()} for item in comparisons
        )
    latency = sorted(
        item["latency_ms"]
        for item in comparisons
        if isinstance(item["latency_ms"], (int, float)) and math.isfinite(item["latency_ms"])
    )
    summary = {
        "questions": len(comparisons),
        "task_success": sum(item["task_success"] for item in comparisons),
        "task_success_rate": sum(item["task_success"] for item in comparisons) / len(comparisons)
        if comparisons
        else None,
        "comparison_verdicts": dict(Counter(item["comparison_verdict"] for item in comparisons)),
        "answer_statuses": dict(Counter(str(item["answer_status"]) for item in comparisons)),
        "execution_statuses": dict(Counter(item["execution_status"] for item in comparisons)),
        "status_vs_comparison": dict(
            Counter(f"{item['answer_status']}:{item['comparison_verdict']}" for item in comparisons)
        ),
        "judge_errors": sum(item["judge_errors"] for item in comparisons),
        "fact_recall_measured": sum(
            item["reference_fact_recall"] is not None for item in comparisons
        ),
        "latency_measured": len(latency),
        "latency_ms_p50": median(latency) if latency else None,
        "latency_ms_p95": latency[math.ceil(len(latency) * 0.95) - 1] if latency else None,
        "tool_metrics_measured": sum(
            item["tool_trace_status"] == "complete" for item in comparisons
        ),
        "token_metrics_measured": sum(
            item["token_usage_status"] == "measured" for item in comparisons
        ),
        "agent_cost_status": "estimated"
        if comparisons and all(item["agent_cost_status"] == "estimated" for item in comparisons)
        else "partial"
        if any(item["agent_cost_status"] in {"estimated", "partial"} for item in comparisons)
        else "unmeasured",
        "agent_cost_known_subtotals": {
            currency: str(
                sum(
                    Decimal(item["agent_cost_known_subtotal"])
                    for item in comparisons
                    if item.get("cost_currency") == currency
                )
            )
            for currency in {
                item["cost_currency"] for item in comparisons if item.get("cost_currency")
            }
        },
        "production_deployed": False,
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (directory / "summary.md").write_text(
        "---\ntitle: Stakeholder Evaluation Results\n"
        "description: Execution, comparison and telemetry coverage for this evaluation run.\n"
        f"ms.date: {datetime.now(UTC).date().isoformat()}\n---\n\n"
        "## Summary\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n\n"
        "See [comparisons.csv](comparisons.csv) for the side-by-side review and "
        "[comparisons.jsonl](comparisons.jsonl) for lossless records.\n\n"
        "Unmeasured mandatory checks block task success. Not-evaluated rows are not "
        "measured answer-quality failures. No production deployment was performed.\n",
        encoding="utf-8",
    )
    return summary


async def run_compare(args: argparse.Namespace) -> int:
    from .__main__ import load_golden
    from .copilot_judge import CopilotCLIJudge, CopilotJudge, CopilotNodeSDKJudge
    from .score import read_records

    questions = load_golden(args.golden)
    if args.only:
        unknown = set(args.only) - {item.id for item in questions}
        if unknown:
            raise ValueError(f"Unknown selected question IDs: {sorted(unknown)}")
        questions = [item for item in questions if item.id in args.only]
    records = read_records(Path(args.answers).read_text(encoding="utf-8").splitlines())
    price_book = json.loads(Path(args.price_book).read_text()) if args.price_book else None
    if args.telemetry:
        telemetry_rows = read_records(Path(args.telemetry).read_text(encoding="utf-8").splitlines())
        telemetry = {item["request_id"]: item for item in telemetry_rows}
        if len(telemetry) != len(telemetry_rows):
            raise ValueError("Duplicate telemetry request IDs")
        for record in records:
            trace = telemetry.get(record.get("request_id"), {})
            for key in (
                "tool_invocations",
                "tool_trace_status",
                "model_usage",
                "model_usage_status",
            ):
                if key in trace:
                    record[key] = trace[key]
    for record in records:
        record["cost_metrics"] = estimated_cost(record, price_book)
    if args.only:
        records = [item for item in records if item["id"] in args.only]
    directory = Path(args.out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset_sha256": hashlib.sha256(Path(args.golden).read_bytes()).hexdigest(),
        "answers_sha256": hashlib.sha256(Path(args.answers).read_bytes()).hexdigest(),
        "model": args.model,
        "transport": args.transport,
        "rubric": RUBRIC_VERSION,
        "repeats": args.repeats,
        "only": sorted(args.only or []),
        "schema": "comparison-v1",
        "telemetry_sha256": hashlib.sha256(Path(args.telemetry).read_bytes()).hexdigest()
        if args.telemetry
        else None,
        "price_book": price_book,
    }
    manifest_path = directory / "run-metadata.json"
    if manifest_path.exists():
        if not args.resume or json.loads(manifest_path.read_text()) != manifest:
            raise ValueError(
                "Existing run differs or --resume was not specified; use a new directory"
            )
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    attempts_path = directory / "judge-attempts.jsonl"
    cached = (
        read_records(attempts_path.read_text(encoding="utf-8").splitlines())
        if attempts_path.exists()
        else []
    )
    with attempts_path.open("a", encoding="utf-8") as stream:

        def emit(item: dict[str, Any]) -> None:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            stream.flush()

        if args.no_judge:
            comparisons = await compare_records(questions, records, None)
        else:
            if not args.confirm_copilot or not args.model or args.model == "auto":
                raise ValueError("Specify --confirm-copilot and an explicit --model (not auto)")
            factories: dict[
                str, type[CopilotCLIJudge] | type[CopilotJudge] | type[CopilotNodeSDKJudge]
            ] = {"cli": CopilotCLIJudge, "sdk": CopilotJudge, "sdk-node": CopilotNodeSDKJudge}
            factory = factories[args.transport]
            async with factory(args.model, timeout=args.timeout) as judge:
                comparisons = await compare_records(
                    questions,
                    records,
                    judge,
                    repeats=args.repeats,
                    emit=emit,
                    cached=cached,
                    model=args.model,
                    concurrency=args.concurrency,
                )
    summary = write_report(directory, comparisons)
    print(json.dumps(summary))
    return 1 if args.gate and any(not item["task_success"] for item in comparisons) else 0


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("compare-answers", help=__doc__)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--answers", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--transport", choices=["cli", "sdk", "sdk-node"], default="cli")
    parser.add_argument("--telemetry", help="Optional observed per-request tool/model usage JSONL")
    parser.add_argument(
        "--price-book", help="Versioned per-served-model price JSON; never inferred"
    )
    parser.add_argument("--confirm-copilot", action="store_true")
    parser.add_argument(
        "--no-judge", action="store_true", help="Inventory only; never reports task success"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only", action="append")
    parser.add_argument("--repeats", type=int, choices=[1, 3, 5], default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--gate", action="store_true")
