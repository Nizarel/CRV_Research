"""Extraction audit: why key facts shown to the extractor never reach the claim ledger.

Inputs come from one answer-eval run with ``-Diagnostics -ExportStages``: the answers JSONL
(per-question ``audit`` with every claim the extractor returned, its outcome and validation
errors, plus the passage IDs each facet saw), the stages JSONL (``<ARM>-EXPOSED`` passage text)
and the shared scorer's JSON (per-fact ``unsupported`` lists for the answer, exposed and ledger
arms). A fact supported by the exposed passages but not by the ledger was lost at extraction;
each such fact gets one bucket:

* ``judge_noise``: an accepted claim carries the fact's anchors (the ledger judge missed it);
* ``validation``: only rejected claims carry it (``detail`` = first error);
* ``cap``: only claims cut by the per-round claim cap carry it;
* ``facet_failed``: the passages holding it were seen only by failed facets;
* ``facet_scope`` / ``extractor_skip``: no claim; the optional LLM check says whether any facet
  that saw the passage asked for the fact (``not_extracted`` without the check);
* ``unlocated``: the fact has no anchors, or no exposed passage holds all of them.

Rejected quotes are also re-tested under whitespace/Unicode normalization, and rejected numbers
under grouped-thousands parsing, to size a validation fix before writing it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...adapters.azure_ai.structured import structured
from ...domain.crv_kg.study import normalize_text as normalize
from .dataset import AnchorText, GoldenQuestion, KeyFact, number_text

_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_SCOPE_INSTRUCTIONS = (
    "You audit a research agent. The agent split a user QUESTION into numbered FACETS and ran "
    "one evidence extractor per facet. For the KEY FACT, list the numbers of the facets whose "
    "question asks for that fact (its quantity, subject or aspect), or an empty list if none "
    "does. Judge only the facet wording, not whether the fact is true."
)
_SCOPE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facets"],
    "properties": {"facets": {"type": "array", "items": {"type": "integer"}}},
}


@dataclass
class LostFact:
    id: str
    category: str
    fact: int
    text: str
    anchors: str
    bucket: str
    detail: str = ""
    passages: list[str] = field(default_factory=list)
    facets_seen: list[int] = field(default_factory=list)
    best_rank: int | None = None
    shown: int | None = None
    claim_quote: str = ""
    normalized_quote_match: bool | None = None
    normalized_number_match: bool | None = None


def error_kind(error: str) -> str:
    lowered = error.lower()
    # "numeric ... in its quotation" must not count as a quote error.
    for key, kind in (
        ("numeric", "number"),
        ("quotation", "quote"),
        ("locator", "locator"),
        ("missing evidence", "evidence"),
        ("calculation", "calculation"),
    ):
        if key in lowered:
            return kind
    return "other"


def fact_stage(fact: int, scores: dict[str, dict[str, Any]], arm: str) -> str | None:
    """Funnel stage of one fact from the per-arm judge votes (None when unmeasured)."""

    exposed, ledger, answer = (
        scores.get(f"{arm}{suffix}") for suffix in ("-EXPOSED", "-LEDGER", "")
    )
    if exposed is None or ledger is None or exposed.get("judge_recall") is None:
        return None
    if fact in (exposed.get("unsupported") or []):
        return "not_exposed"
    if ledger.get("judge_recall") is None or fact in (ledger.get("unsupported") or []):
        return "lost_at_extraction"
    if answer is not None and fact in (answer.get("unsupported") or []):
        return "lost_at_answer"
    return "answered"


def carries(fact: KeyFact, text: str) -> bool:
    """All anchors of a short fact; at least half of a multi-value fact's (stated piecewise)."""

    prepared = AnchorText(text)
    found = sum(prepared.has(anchor) for anchor in fact.anchors)
    need = len(fact.anchors) if len(fact.anchors) <= 2 else -(-len(fact.anchors) // 2)
    return bool(fact.anchors) and found >= need


def rescues(claim: dict[str, Any], passages: dict[str, str]) -> tuple[bool | None, bool | None]:
    """Would normalized quote matching / grouped-thousands numbers accept this claim?"""

    kinds = {error_kind(error) for error in claim.get("errors") or []}
    cited = [passages[i] for i in claim.get("evidence_ids") or [] if i in passages]
    quote_ok = None
    if "quote" in kinds and cited:
        quote = normalize(str(claim.get("quote") or ""))
        quote_ok = bool(quote) and any(quote in normalize(text) for text in cited)
    number_ok = None
    if "number" in kinds and claim.get("value") is not None:
        target = float(claim["value"])
        number_ok = any(
            abs(float(found) - target) < 1e-9
            for found in _NUMBER.findall(number_text(str(claim.get("quote") or "")))
        )
    return quote_ok, number_ok


def classify(
    question: GoldenQuestion,
    fact: int,
    audit: Sequence[dict[str, Any]],
    passages: dict[str, str],
    scope: Sequence[int] | None = None,
) -> LostFact:
    """Bucket one fact lost at extraction (``scope``: facet numbers that ask for it)."""

    key = question.key_facts[fact - 1]
    lost = LostFact(
        id=question.id,
        category=question.category,
        fact=fact,
        text=key.text,
        anchors=" ".join(key.anchors),
        bucket="unlocated",
    )
    claims = [
        claim
        for facet in audit
        for claim in facet.get("claims") or []
        if carries(key, f"{claim.get('text', '')}\n{claim.get('quote', '')}")
    ]
    by_outcome = defaultdict(list)
    for claim in claims:
        by_outcome[claim.get("outcome")].append(claim)
    if by_outcome["accepted"]:
        lost.bucket, lost.claim_quote = "judge_noise", str(by_outcome["accepted"][0]["quote"])
        return lost
    if by_outcome["rejected"]:
        claim = by_outcome["rejected"][0]
        lost.bucket, lost.claim_quote = "validation", str(claim.get("quote") or "")
        lost.detail = error_kind((claim.get("errors") or ["other"])[0])
        lost.normalized_quote_match, lost.normalized_number_match = rescues(claim, passages)
        return lost
    if by_outcome["capped"]:
        lost.bucket, lost.claim_quote = "cap", str(by_outcome["capped"][0]["quote"])
        return lost
    lost.passages = sorted(pid for pid, text in passages.items() if carries(key, text))
    if not lost.passages:
        return lost
    ranks = []
    for number, facet in enumerate(audit, start=1):
        for extraction in facet.get("rounds") or []:
            shown = list(extraction.get("passage_ids") or [])
            hits = [shown.index(pid) + 1 for pid in lost.passages if pid in shown]
            if hits:
                lost.facets_seen.append(number)
                ranks.append((min(hits), len(shown)))
    lost.facets_seen = sorted(set(lost.facets_seen))
    if ranks:
        lost.best_rank, lost.shown = min(ranks)
    if lost.facets_seen and all(
        audit[n - 1].get("status") != "completed" for n in lost.facets_seen
    ):
        lost.bucket = "facet_failed"
    elif scope is None:
        lost.bucket = "not_extracted"
    else:
        asked = set(scope) & set(lost.facets_seen)
        lost.bucket = "extractor_skip" if asked else "facet_scope"
        lost.detail = ",".join(str(n) for n in sorted(asked))
    return lost


def claim_totals(
    rows: Sequence[dict[str, Any]], passages: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Outcomes of every returned claim, rejection reasons, and how many a fix would rescue."""

    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    rescued: Counter[str] = Counter()
    for row in rows:
        for facet in row.get("audit") or []:
            for claim in facet.get("claims") or []:
                outcomes[str(claim.get("outcome"))] += 1
                if claim.get("outcome") != "rejected":
                    continue
                kinds = sorted({error_kind(e) for e in claim.get("errors") or []})
                reasons["+".join(kinds) or "other"] += 1
                quote_ok, number_ok = rescues(claim, passages.get(row["id"], {}))
                if quote_ok:
                    rescued["quote_normalized"] += 1
                if number_ok:
                    rescued["number_normalized"] += 1
    return {"outcomes": dict(outcomes), "rejections": dict(reasons), "rescuable": dict(rescued)}


def exposed_passages(stages: Sequence[dict[str, Any]], arm: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = defaultdict(dict)
    for record in stages:
        if record.get("arm") == f"{arm}-EXPOSED":
            for context in record.get("contexts") or []:
                found[record["id"]][str(context["id"])] = str(context.get("text") or "")
    return found


async def facet_scope(
    client: Any,
    deployment: str,
    question: GoldenQuestion,
    facets: Sequence[str],
    fact: KeyFact,
) -> list[int]:
    listed = "\n".join(f"{n}. {text}" for n, text in enumerate(facets, start=1))
    parsed = await structured(
        client,
        deployment,
        _SCOPE_INSTRUCTIONS,
        f"QUESTION: {question.question}\n\nFACETS:\n{listed}\n\nKEY FACT: {fact.text}",
        "facet_scope",
        _SCOPE_SCHEMA,
    )
    return sorted({int(n) for n in parsed.get("facets", []) if 1 <= int(n) <= len(facets)})


def read_jsonl(path: str) -> list[dict[str, Any]]:
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    return [json.loads(line) for line in lines if line.strip().startswith("{")]


async def audit_run(
    questions: dict[str, GoldenQuestion],
    answers: Sequence[dict[str, Any]],
    stages: Sequence[dict[str, Any]],
    scores: Sequence[dict[str, Any]],
    arm: str,
    client: Any = None,
    deployment: str = "",
    concurrency: int = 4,
) -> tuple[list[LostFact], dict[str, Any]]:
    by_question: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in scores:
        by_question[item["id"]][item["arm"]] = item
    passages = exposed_passages(stages, arm)
    rows = [row for row in answers if row.get("arm") == arm and row.get("audit") is not None]
    stages_by_category: dict[str, Counter[str]] = defaultdict(Counter)
    pending: list[tuple[GoldenQuestion, int, dict[str, Any]]] = []
    for row in rows:
        question = questions[row["id"]]
        for fact in range(1, len(question.key_facts) + 1):
            stage = fact_stage(fact, by_question[row["id"]], arm)
            if stage is None:
                continue
            stages_by_category[question.category][stage] += 1
            stages_by_category["ALL"][stage] += 1
            if stage == "lost_at_extraction":
                pending.append((question, fact, row))
    gate = asyncio.Semaphore(concurrency)

    async def one(question: GoldenQuestion, fact: int, row: dict[str, Any]) -> LostFact:
        audit = row.get("audit") or []
        scope = None
        if client is not None:
            facets = [str(entry.get("facet") or "") for entry in audit]
            async with gate:
                scope = await facet_scope(
                    client,
                    deployment,
                    question,
                    facets,
                    question.key_facts[fact - 1],
                )
        return classify(question, fact, audit, passages.get(row["id"], {}), scope)

    lost = list(await asyncio.gather(*(one(*item) for item in pending)))
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in lost:
        buckets[entry.category][entry.bucket] += 1
        buckets["ALL"][entry.bucket] += 1
    summary = {
        "arm": arm,
        "questions": len(rows),
        "stages": {k: dict(v) for k, v in sorted(stages_by_category.items())},
        "buckets": {k: dict(v) for k, v in sorted(buckets.items())},
        "validation_detail": dict(Counter(i.detail for i in lost if i.bucket == "validation")),
        "claims": claim_totals(rows, passages),
    }
    return lost, summary


def write_csv(path: str, lost: Sequence[LostFact]) -> None:
    columns = list(LostFact.__dataclass_fields__)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in lost:
            row = asdict(item)
            row["passages"] = " ".join(item.passages)
            row["facets_seen"] = " ".join(str(n) for n in item.facets_seen)
            writer.writerow(row)


async def run_audit(args: argparse.Namespace) -> int:
    from .__main__ import load_golden

    questions = {q.id: q for q in load_golden(args.golden)}
    score = json.loads(Path(args.score).read_text(encoding="utf-8-sig"))
    client, credential = None, None
    if args.judge_deployment:
        from azure.identity.aio import DefaultAzureCredential

        from ...adapters.azure_ai.embeddings import create_openai_client

        credential = DefaultAzureCredential()
        client = create_openai_client(
            args.openai_endpoint, credential, api_version=args.api_version
        )
    try:
        lost, summary = await audit_run(
            questions,
            read_jsonl(args.answers),
            read_jsonl(args.stages) if args.stages else [],
            score["scores"],
            args.arm,
            client,
            args.judge_deployment,
            args.concurrency,
        )
    finally:
        if credential is not None:
            await credential.close()
    write_csv(args.out_csv, lost)
    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def add_parser(commands: Any) -> None:
    audit = commands.add_parser(
        "audit",
        help="Bucket key facts lost between exposed passages and the claim ledger",
    )
    audit.add_argument("--golden", required=True)
    audit.add_argument("--answers", required=True, help="answers.jsonl with per-question audit")
    audit.add_argument("--stages", default="", help="stages.jsonl (exposed passage text)")
    audit.add_argument("--score", required=True, help="score.json with exposed/ledger arms")
    audit.add_argument("--arm", required=True)
    audit.add_argument(
        "--judge-deployment",
        default="",
        help="Optional LLM facet-scope check (splits not_extracted)",
    )
    audit.add_argument("--openai-endpoint", default="")
    audit.add_argument("--api-version", default="2025-04-01-preview")
    audit.add_argument("--concurrency", type=int, default=4)
    audit.add_argument("--out-csv", required=True)
    audit.add_argument("--out-json", default="")
