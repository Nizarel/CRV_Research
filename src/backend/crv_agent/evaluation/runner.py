import json
import time
from collections import defaultdict
from math import ceil
from statistics import mean
from uuid import uuid4

import httpx

from crv_agent.domain.answers import Answer
from crv_agent.evaluation.dataset import GoldenQuestion
from crv_agent.evaluation.gates import gate_failures
from crv_agent.evaluation.judge import FactJudge
from crv_agent.evaluation.scoring import score


async def evaluate(
    client: httpx.AsyncClient, questions: list[GoldenQuestion], judge: FactJudge | None = None,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    measurements: dict[str, list[float | bool | None]] = defaultdict(list)
    latencies: list[float] = []
    for question in questions:
        created = await client.post("/api/v1/conversations")
        created.raise_for_status()
        thread = created.json()
        started = time.monotonic()
        first_progress: float | None = None
        first_answer: float | None = None
        answer: Answer | None = None
        async with client.stream("POST", "/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": question.question,
        }) as response:
            response.raise_for_status()
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    if event == "progress" and first_progress is None:
                        first_progress = time.monotonic() - started
                    elif event == "answer":
                        first_answer = time.monotonic() - started
                        answer = Answer.model_validate({
                            key: value for key, value in data.items()
                            if key not in {"run_id", "sequence"}
                        })
                    elif event == "error":
                        raise RuntimeError(f"Question {question.id} failed: {data['error']}")
        if answer is None or first_answer is None:
            raise RuntimeError(f"Question {question.id} returned no verified answer")
        scores = score(question, answer)
        if judge is not None and question.key_facts:
            grades = await judge.grade([fact.text for fact in question.key_facts], answer.content)
            scores["key_fact_recall"] = mean(grades)
            if question.category == "A":
                scores["key_fact_recall_A"] = scores["key_fact_recall"]
            elif question.category in {"B", "C"}:
                scores["key_fact_recall_BC"] = scores["key_fact_recall"]
        for metric, value in scores.items():
            measurements[metric].append(value)
        latencies.append(first_answer)
        records.append({
            "id": question.id, "category": question.category, "split": question.split,
            "search_release": answer.search_release, "kg_release": answer.kg_release,
            "scores": scores, "answer_status": answer.status,
            "retrieval_mode": answer.retrieval_mode,
            "first_progress_seconds": first_progress, "first_answer_seconds": first_answer,
            "total_seconds": time.monotonic() - started,
        })
    p95 = sorted(latencies)[ceil(len(latencies) * 0.95) - 1] if latencies else None
    failures = gate_failures(measurements, p95)
    return {
        "questions": len(records), "records": records,
        "metrics": {name: measured_rate(values) for name, values in measurements.items()},
        "first_answer_seconds_p95": p95,
        "gate_failures": failures, "gates_passed": not failures,
    }


def measured_rate(values: list[float | bool | None]) -> dict[str, object]:
    measured = [value for value in values if value is not None]
    return {
        "rate": mean(measured) if measured else None,
        "measured": len(measured), "unmeasured": len(values) - len(measured),
    }
