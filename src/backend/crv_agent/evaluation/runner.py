"""End-to-end answer evaluation over the real API (SSE), with per-question failure capture.

Failed or cancelled turns are recorded and count against every metric; they never abort the
run or silently shrink the denominator. Answers can be exported in the shared context format so
`ingest_pipeline.tools.golden score` judges them with the same judge as retrieval-depth runs.
"""

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Callable
from math import ceil
from statistics import mean, median
from typing import Any
from uuid import uuid4

import httpx

from crv_agent.domain.answers import Answer
from crv_agent.evaluation.dataset import GoldenQuestion
from crv_agent.evaluation.gates import gate_failures
from crv_agent.evaluation.judge import FactJudge
from crv_agent.evaluation.scoring import score


async def ask(client: httpx.AsyncClient, question: str) -> dict[str, Any]:
    started = time.monotonic()
    first_progress: float | None = None
    first_finding: float | None = None
    first_answer: float | None = None
    answer: Answer | None = None
    error: str | None = None
    steps: list[str] = []
    progress: list[tuple[str, float]] = []
    diagnostics: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    try:
        created = await client.post("/api/v1/conversations")
        created.raise_for_status()
        async with client.stream("POST", "/api/v1/chat", json={
            "thread_id": created.json()["id"], "request_id": str(uuid4()), "message": question,
        }) as response:
            response.raise_for_status()
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    if event == "progress":
                        elapsed = time.monotonic() - started
                        first_progress = first_progress or elapsed
                        steps.append(str(data.get("step")))
                        progress.append((str(data.get("step")), elapsed))
                    elif event == "diagnostics":
                        diagnostics = {
                            "ledger": data["ledger"], "exposed": data["exposed"],
                            "audit": data.get("audit") or [],
                        }
                    elif event == "plan":
                        plan = {"intent": data.get("intent"), "facets": data.get("facets") or []}
                    elif event == "facet" and data.get("claims"):
                        first_finding = first_finding or time.monotonic() - started
                    elif event == "answer":
                        first_answer = time.monotonic() - started
                        answer = Answer.model_validate({
                            key: value for key, value in data.items()
                            if key not in {"run_id", "sequence"}
                        })
                    elif event == "error":
                        error = str(data.get("error_type") or data.get("error"))
        if answer is None and error is None:
            error = "no_verified_answer"
    except (httpx.HTTPError, ValueError) as exc:
        error = f"{type(exc).__name__}"
    return {
        "answer": answer, "error": error, "steps": steps,
        "step_seconds": step_durations(progress), "diagnostics": diagnostics, "plan": plan,
        "first_progress_seconds": first_progress, "first_finding_seconds": first_finding,
        "first_answer_seconds": first_answer,
        "total_seconds": time.monotonic() - started,
    }


def step_durations(progress: list[tuple[str, float]]) -> dict[str, float]:
    """Seconds spent in each graph step, from consecutive progress timestamps."""
    durations: dict[str, float] = {}
    previous = 0.0
    for step, at in progress:
        if step != "started":
            durations[step] = round(durations.get(step, 0.0) + at - previous, 3)
        previous = at
    return durations


def compact(
    question: GoldenQuestion, result: dict[str, Any], arm: str = "AGENT",
) -> dict[str, Any]:
    """One answer record without source text: reviewable, and scoreable as a context."""
    answer: Answer | None = result["answer"]
    content = answer.content if answer else ""
    citations = answer.citations if answer else []
    sources = [c.source_path or c.source_uri for c in citations]
    return {
        "id": question.id, "category": question.category, "split": question.split,
        "arm": arm, "passage_mode": "split", "error": None,
        "turn_error": result["error"], "status": answer.status if answer else "error",
        "seconds": round(result["first_answer_seconds"] or result["total_seconds"], 3),
        "first_progress_seconds": result["first_progress_seconds"],
        "first_finding_seconds": result["first_finding_seconds"],
        "step_seconds": result["step_seconds"],
        "usage": answer.usage if answer else {},
        "role_usage": {
            role: usage.model_dump() for role, usage in answer.role_usage.items()
        } if answer else {},
        "exposed_characters": len(content),
        "documents": len({c.content_hash for c in citations}),
        "secondary_share": round(
            sum(c.source_role == "secondary_summary" for c in citations) / len(citations), 3,
        ) if citations else None,
        "dossier_cu_share": round(
            sum("dossier cu" in s.casefold() for s in sources) / len(sources), 3,
        ) if sources else None,
        "steps": result["steps"],
        "plan": result.get("plan"),
        # Extraction audit (diagnostics runs only): claims with outcomes, passage IDs, no text.
        "audit": (result.get("diagnostics") or {}).get("audit"),
        "answer": {
            "content": content,
            "claims": [
                {"text": c.text, "evidence_ids": c.evidence_ids, "quote": c.quote,
                 "value": str(c.value) if c.value is not None else None, "unit": c.unit,
                 "calculation_index": c.calculation_index}
                for c in (answer.claims if answer else [])
            ],
            "citations": [
                {"id": c.id, "source": c.source_path or c.source_uri, "pages": c.pages,
                 "document_code": c.document_code, "source_role": c.source_role}
                for c in citations
            ],
            "conflicts": [c.model_dump() for c in (answer.conflicts if answer else [])],
            "calculations": [
                c.model_dump(mode="json") for c in (answer.calculations if answer else [])
            ],
            "coverage_gaps": answer.coverage.gaps if answer else [],
            "reasons": answer.reasons if answer else [],
        },
        # The judged "context" is the answer text only (an empty answer scores zero).
        "contexts": [{
            "id": "answer", "content_hash": "", "title": "answer", "source": "answer",
            "pages": [], "text": content,
        }],
    }


_EMPTY_CONTEXT = {"id": "empty", "content_hash": "", "title": "", "source": "", "pages": [],
                  "text": ""}


def stage_records(
    question: GoldenQuestion, result: dict[str, Any], arm: str = "AGENT",
) -> list[dict[str, Any]]:
    """Exposed passages and the claim ledger as extra scorer arms, to locate lost facts."""
    found: dict[str, Any] | None = result.get("diagnostics")
    if found is None:
        return []
    base: dict[str, Any] = {
        "id": question.id, "category": question.category, "split": question.split,
        "passage_mode": "split", "error": None, "seconds": None, "usage": {},
        "secondary_share": None, "dossier_cu_share": None,
    }
    exposed = [
        {key: passage[key] for key in ("id", "content_hash", "title", "source", "pages", "text")}
        for passage in found["exposed"]
    ]
    ledger = [
        {
            "id": f"claim-{n}", "content_hash": "", "title": claim["subject"],
            "source": "ledger", "pages": [], "text": f"{claim['text']}\n{claim['quote']}",
        } for n, claim in enumerate(found["ledger"])
    ]
    return [
        {
            **base, "arm": f"{arm}-EXPOSED",
            "exposed_characters": sum(len(p["text"]) for p in exposed),
            "documents": len({p["content_hash"] for p in exposed}),
            "contexts": exposed or [_EMPTY_CONTEXT],
        },
        {
            **base, "arm": f"{arm}-LEDGER",
            "exposed_characters": sum(len(c["text"]) for c in ledger),
            "documents": len({e for c in found["ledger"] for e in c["evidence_ids"]}),
            "contexts": ledger or [_EMPTY_CONTEXT],
        },
    ]


def role_summary(records: list[dict[str, Any]]) -> dict[str, dict[str, object]]:
    """Mean per-turn usage per deployment role over completed turns, plus served models."""
    completed = [r["role_usage"] for r in records if r["answer_status"] != "error"]
    summary: dict[str, dict[str, object]] = {}
    for role in sorted({role for usage in completed for role in usage}):
        rows = [usage.get(role, {}) for usage in completed]
        models: dict[str, int] = defaultdict(int)
        for row in rows:
            for name, calls in row.get("models", {}).items():
                models[name] += calls
        summary[role] = {
            **{
                key: round(mean(row.get(key, 0) for row in rows), 3)
                for key in (
                    "calls", "seconds", "input_tokens", "output_tokens", "reasoning_tokens",
                    "cached_tokens",
                )
            },
            "models": dict(models),
        }
    return summary


async def evaluate(
    client: httpx.AsyncClient, questions: list[GoldenQuestion], judge: FactJudge | None = None,
    *, concurrency: int = 1, emit: Callable[[dict[str, Any]], None] | None = None,
    arm: str = "AGENT", emit_stages: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, object]:
    gate = asyncio.Semaphore(concurrency)
    records: list[dict[str, Any]] = []
    measurements: dict[str, list[float | bool | None]] = defaultdict(list)
    latencies: list[float] = []
    finding_latencies: list[float] = []

    async def one(question: GoldenQuestion) -> None:
        async with gate:
            result = await ask(client, question.question)
        answer: Answer | None = result["answer"]
        if answer is not None:
            scores = score(question, answer)
        else:
            scores = {
                "citation_contract_valid": None, "source_match": False if
                question.expected_sources else None, "numeric_exact": False if
                question.numeric_checks else None,
                "abstention_correct": False if question.must_abstain else None,
                "conflict_flagged": False if question.must_flag_conflict else None,
                "calculator_used": False if question.category == "G" else None,
                "catalogue_used": False if question.category == "E" else None,
                "key_fact_recall": None,
            }
        scores["turn_completed"] = answer is not None
        judge_error: str | None = None
        if judge is not None and question.key_facts and not question.must_abstain:
            grades: list[bool] | None = [False] * len(question.key_facts)
            if answer is not None:
                try:
                    grades = await judge.grade(
                        [fact.text for fact in question.key_facts], answer.content,
                    )
                except Exception as error:  # recorded; the question stays unmeasured
                    judge_error, grades = type(error).__name__, None
            recall = mean(grades) if grades is not None else None
            scores["key_fact_recall"] = recall
            if question.category == "A":
                scores["key_fact_recall_A"] = recall
            elif question.category in {"B", "C"}:
                scores["key_fact_recall_BC"] = recall
        for metric, value in scores.items():
            measurements[metric].append(value)
        # Failed turns count with the full elapsed time; they are not dropped from latency.
        latencies.append(result["first_answer_seconds"] or result["total_seconds"])
        # Without a streamed finding, the first useful content is the answer itself.
        finding_latencies.append(
            result["first_finding_seconds"] or result["first_answer_seconds"]
            or result["total_seconds"],
        )
        records.append({
            "id": question.id, "category": question.category, "split": question.split,
            "scores": scores, "answer_status": answer.status if answer else "error",
            "turn_error": result["error"], "judge_error": judge_error,
            "retrieval_mode": answer.retrieval_mode if answer else None,
            "usage": answer.usage if answer else {},
            "role_usage": {
                role: usage.model_dump() for role, usage in answer.role_usage.items()
            } if answer else {},
            "step_seconds": result["step_seconds"],
            "first_progress_seconds": result["first_progress_seconds"],
            "first_finding_seconds": result["first_finding_seconds"],
            "first_answer_seconds": result["first_answer_seconds"],
            "total_seconds": result["total_seconds"],
        })
        record = compact(question, result, arm)
        if emit is not None:
            emit(record)
        if emit_stages is not None:
            emit_stages([record, *stage_records(question, result, arm)])

    await asyncio.gather(*(one(question) for question in questions))
    records.sort(key=lambda record: int(str(record["id"]).lstrip("Q") or 0))
    p95 = sorted(latencies)[ceil(len(latencies) * 0.95) - 1] if latencies else None
    p50 = sorted(latencies)[len(latencies) // 2] if latencies else None
    failures = gate_failures(measurements, p95)
    durations: dict[str, list[float]] = defaultdict(list)
    for record in records:
        for step, seconds in record["step_seconds"].items():
            durations[step].append(seconds)
    return {
        "arm": arm, "questions": len(records), "records": records,
        "metrics": {name: measured_rate(values) for name, values in measurements.items()},
        "statuses": {
            status: sum(1 for r in records if r["answer_status"] == status)
            for status in sorted({str(r["answer_status"]) for r in records})
        },
        "turn_errors": {
            error: sum(1 for r in records if r["turn_error"] == error)
            for error in sorted({str(r["turn_error"]) for r in records if r["turn_error"]})
        },
        "first_answer_seconds_p50": p50, "first_answer_seconds_p95": p95,
        "first_finding_seconds_p50": percentile(finding_latencies, 0.5),
        "first_finding_seconds_p95": percentile(finding_latencies, 0.95),
        "step_seconds_p50": {
            step: round(median(values), 3) for step, values in sorted(durations.items())
        },
        "role_usage_mean": role_summary(records),
        "gate_failures": failures, "gates_passed": not failures,
    }


def percentile(values: list[float], share: float) -> float | None:
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * share) - 1)] if ordered else None


def measured_rate(values: list[float | bool | None]) -> dict[str, object]:
    measured = [value for value in values if value is not None]
    return {
        "rate": mean(measured) if measured else None,
        "measured": len(measured), "unmeasured": len(values) - len(measured),
    }
