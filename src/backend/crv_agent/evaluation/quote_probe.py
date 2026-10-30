"""Quote-fidelity probe: how often a worker model copies source quotations exactly.

Three steps, each writing JSONL that holds CRV text (keep outputs on the encrypted volume):

* ``freeze`` retrieves, for a stratified subset of golden questions, the passages the
  production extractor would show (same search, pack policy and passage format).
* ``run`` sends those frozen passages to one or more chat models through the production
  extraction prompt and strict schema, recording claims, parse errors, latency and usage.
* ``score`` (offline, no model calls) anchors and validates every claim with the production
  code (``anchor_claim`` then ``claim_errors``) and classifies each rejected quotation, so
  near misses a normalization fix could recover are separated from real paraphrase.

Re-running ``score`` after changing the anchoring code measures that change with no new calls.
"""

import argparse
import asyncio
import difflib
import hashlib
import json
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.services.anchoring import anchor_claim, locate, unescape
from crv_agent.services.evidence_pack import exposed_spans, exposed_text
from crv_agent.services.verification import claim_errors

QUOTE_ERROR = "exact quotation"
_LINE_HYPHEN = re.compile(r"(?<=\w)[-\u00ad\u2010\u2011]\s*\n\s*(?=\w)")
_SPACED_HYPHEN = re.compile(r"(?<=\w)-\s+(?=\w)")
_PARTS = re.compile(r"\s*(?:\n+|\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.|(?<=[.;:])\s+)\s*")
_WORD = re.compile(r"\w+")
MIN_PART = 8
NEAR_COPY = 0.6
PARTIAL_COPY = 0.2
PARAPHRASE_WORDS = 0.6


# ---------------------------------------------------------------------------------------------
# Classification (pure; unit-tested with synthetic strings)
# ---------------------------------------------------------------------------------------------

def join_line_hyphens(text: str) -> str:
    """Words split by a hyphen at a line break (PDF text), joined; spaced hyphens too."""
    return _SPACED_HYPHEN.sub("", _LINE_HYPHEN.sub("", text))


def strip_accents(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )


def _found(quote: str, texts: Sequence[str]) -> bool:
    return bool(quote.strip()) and any(locate(quote, text) for text in texts)


def _words(text: str) -> list[str]:
    return _WORD.findall(strip_accents(text).lower())


def _grams(words: list[str], size: int) -> set[tuple[str, ...]]:
    size = min(size, len(words))
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)} if size else set()


def containment(quote: str, text: str, size: int = 3) -> float:
    """Share of the quote's word n-grams present in the text (accent and case folded)."""
    words = _words(quote)
    grams = _grams(words, size)
    if not grams:
        return 0.0
    return len(grams & _grams(_words(text), min(size, len(words)))) / len(grams)


def classify_quote(quote: str, texts: Sequence[str]) -> str:
    """Why a quotation is not found in the shown texts, cheapest explanation first.

    ``located`` means anchoring can already find it. The next three name a normalization the
    anchoring lacks; ``spliced`` joins separately present fragments; the rest grade how much
    of the quote's wording the passages contain.
    """
    if not quote.strip():
        return "empty"
    if _found(quote, texts):
        return "located"
    fixes = (
        ("json_escape", unescape, False),
        ("line_hyphen", join_line_hyphens, True),
        ("accents", strip_accents, True),
    )
    for name, fix, on_source in fixes:
        sources = [fix(text) for text in texts] if on_source else texts
        if fix(quote) != quote or on_source and sources != list(texts):
            if _found(fix(quote), sources):
                return name
    folded_quote = strip_accents(join_line_hyphens(unescape(quote)))
    folded = [strip_accents(join_line_hyphens(text)) for text in texts]
    if _found(folded_quote, folded):
        return "mixed_normalization"
    parts = [
        part for raw in _PARTS.split(folded_quote) if len(part := raw.strip(" .;:,")) >= MIN_PART
    ]
    if len(parts) >= 2 and all(_found(part, folded) for part in parts):
        return "spliced"
    best = max((containment(quote, text) for text in texts), default=0.0)
    if best >= NEAR_COPY:
        return "near_copy"
    if best >= PARTIAL_COPY:
        return "partial_copy"
    words = max((containment(quote, text, 1) for text in texts), default=0.0)
    return "paraphrase" if words >= PARAPHRASE_WORDS else "not_in_passages"


def nearest_window(quote: str, texts: Sequence[str], margin: int = 60) -> str:
    """The source text around the longest run it shares with the quote (for manual review)."""
    best = ""
    best_size = -1
    needle = quote.lower()
    for text in texts:
        matcher = difflib.SequenceMatcher(None, needle, text.lower(), autojunk=False)
        match = matcher.find_longest_match(0, len(needle), 0, len(text))
        if match.size > best_size:
            start = max(0, match.b - match.a - margin)
            best = text[start:match.b - match.a + len(quote) + margin]
            best_size = match.size
    return best


@dataclass(frozen=True)
class Outcome:
    raw_exact: bool
    anchored: bool
    accepted: bool
    category: str
    errors: list[str]
    claim: Claim


def judge_claim(raw: Claim, shown: Sequence[Evidence]) -> Outcome:
    """Production acceptance for one returned claim, plus why it failed."""
    texts = [text for item in shown if not item.generated for text in exposed_text(item)]
    claim = anchor_claim(raw, shown)
    errors = claim_errors(claim, {item.id: item for item in shown})
    if not errors:
        category = "accepted"
    elif any(QUOTE_ERROR in error for error in errors):
        category = classify_quote(raw.quote, texts)
    elif any("numeric value" in error for error in errors):
        category = "value_not_in_quote"
    elif any("locator" in error for error in errors):
        category = "no_locator"
    elif any("missing evidence" in error for error in errors):
        category = "unknown_evidence_id"
    else:
        category = "other"
    return Outcome(
        raw_exact=bool(raw.quote.strip()) and any(raw.quote in text for text in texts),
        anchored=claim != raw, accepted=not errors, category=category, errors=errors,
        claim=claim,
    )


def percentile(values: Sequence[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 2)


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_evidence(record: dict[str, Any]) -> list[Evidence]:
    return [Evidence.model_validate(item) for item in record["evidence"]]


def shown_passages(evidence: Sequence[Evidence]) -> list[dict[str, object]]:
    """Frozen evidence back to the exact passages the extractor saw, in shown order."""
    from crv_agent.orchestration.extraction import passage

    return [passage(item, exposed_spans(item)[0]) for item in evidence]


def select_questions(
    questions: Sequence[Any], per_category: int, only: Iterable[str], include_abstain: bool,
) -> list[Any]:
    wanted = set(only)
    if wanted:
        return [question for question in questions if question.id in wanted]
    taken: Counter[str] = Counter()
    chosen = []
    for question in sorted(questions, key=lambda item: (item.category, item.id)):
        if question.must_abstain and not include_abstain:
            continue
        if taken[question.category] < per_category:
            taken[question.category] += 1
            chosen.append(question)
    return chosen


def parse_model(spec: str) -> tuple[str, str | None]:
    """``name`` or ``name@effort`` (reasoning effort, ``none`` to disable)."""
    name, _, effort = spec.partition("@")
    return name, effort or None


# ---------------------------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------------------------

async def _freeze(args: argparse.Namespace) -> int:
    from crv_agent.domain.execution import Budget
    from crv_agent.evaluation.dataset import load_questions
    from crv_agent.orchestration.extraction import candidate_pack, search_text
    from crv_agent.runtime.infrastructure import infrastructure
    from crv_agent.runtime.settings import Settings, evidence_policy
    from crv_agent.services.evidence_pack import expose
    from crv_agent.services.retrieval import Retrieval, identifiers

    settings = Settings()
    policy = evidence_policy(settings)
    if policy.rerank != "none":
        raise SystemExit("freeze reproduces CRV_EVIDENCE_RERANK=none only")
    questions = select_questions(
        load_questions(Path(args.golden), args.split), args.per_category, args.only or [],
        args.include_abstain,
    )
    gate = asyncio.Semaphore(args.concurrency)
    records: dict[str, dict[str, Any]] = {}
    async with infrastructure(settings, memory=False) as infra:
        retrieval = Retrieval(infra.search)

        async def one(question: Any) -> None:
            # The production facet text, for a facet equal to the question and no premises.
            worker_question = (
                question.question + "\nOriginal question: " + question.question
                + "\nPremises to verify: "
            )
            fixed = identifiers(worker_question)
            text = search_text(worker_question.split("\n", 1)[0], fixed)
            async with gate:
                pack = await candidate_pack(
                    retrieval, text, fixed, settings.search_release, [],
                    Budget(retrieval_limit=40, seconds=600), policy, None, strict=False,
                )
            records[question.id] = {
                "question_id": question.id, "category": question.category,
                "worker_question": worker_question, "search_text": text,
                "release": settings.search_release,
                "search": {
                    "backend": settings.search_backend, "vector_model": settings.pg_vector_model,
                    "reranker": settings.pg_reranker,
                },
                "policy": {
                    "pack_size": policy.pack_size,
                    "excerpt_characters": policy.excerpt_characters, "anchor": policy.anchor,
                },
                "evidence": [
                    expose(hit, span).model_dump(mode="json") for hit, span in pack.shown
                ],
            }
            print(f"froze {question.id}: {len(pack.shown)} passages", file=sys.stderr)

        await asyncio.gather(*(one(question) for question in questions))
    with Path(args.out).open("w", encoding="utf-8") as out:
        for question in questions:
            out.write(json.dumps(records[question.id], ensure_ascii=False) + "\n")
    print(json.dumps({
        "questions": len(questions), "out": args.out, "sha256": sha256(Path(args.out)),
    }))
    return 0


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------

def _done(path: Path) -> set[tuple[str, str, int, int]]:
    if not path.exists():
        return set()
    return {
        (item["question_id"], item["label"], item["batches"], item["batch"])
        for item in read_jsonl(path) if not item.get("error")
    }


async def _run(args: argparse.Namespace) -> int:
    from langchain_openai import ChatOpenAI

    from crv_agent.orchestration.extraction import batched, extraction_messages, extractor
    from crv_agent.runtime.infrastructure import COGNITIVE_SCOPE, create_chat_models
    from crv_agent.runtime.settings import Settings

    settings = Settings()
    frozen_path = Path(args.frozen)
    frozen = read_jsonl(frozen_path)[:args.limit or None]
    digest = sha256(frozen_path)
    out_path = Path(args.out)
    done = _done(out_path)
    gate = asyncio.Semaphore(args.concurrency)
    async with AsyncExitStack() as stack:
        token_provider = None
        if settings.effective_model_auth == "entra":
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            credential = await stack.enter_async_context(DefaultAzureCredential())
            token_provider = get_bearer_token_provider(credential, COGNITIVE_SCOPE)
        models: dict[str, tuple[str, str | None, ChatOpenAI]] = {}
        for spec in args.model:
            name, effort = parse_model(spec)
            configured = settings.model_copy(update={
                "worker_deployment": name, "worker_reasoning_effort": effort,
            })
            models[spec] = (name, effort, create_chat_models(stack, configured, token_provider)[
                "worker"
            ])
        out = stack.enter_context(out_path.open("a", encoding="utf-8"))

        async def call(
            record: dict[str, Any], label: str, batches: int, batch: int,
            group: list[dict[str, object]],
        ) -> None:
            name, effort, model = models[label]
            messages = extraction_messages(
                record["worker_question"], group, 0, args.max_claims, [],
            )
            entry: dict[str, Any] = {
                "question_id": record["question_id"], "category": record["category"],
                "label": label, "model": name, "effort": effort, "batches": batches,
                "batch": batch, "passage_ids": [item["id"] for item in group],
                "frozen_sha256": digest,
            }
            async with gate:
                started = time.perf_counter()
                try:
                    result = await extractor(model, include_raw=True).ainvoke(messages)
                    raw, parsed = result["raw"], result["parsed"]
                    usage = getattr(raw, "usage_metadata", None) or {}
                    entry.update({
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "parsing_error": (
                            str(result["parsing_error"])[:500] if result["parsing_error"] else None
                        ),
                        "claims": [c.model_dump(mode="json") for c in parsed.claims]
                        if parsed else [],
                        "gaps": parsed.gaps if parsed else [],
                        "rewrite": parsed.rewrite if parsed else None,
                        "error": None,
                    })
                except Exception as error:  # recorded per call, never dropped
                    entry.update({"claims": [], "error": f"{type(error).__name__}: {error}"[:500]})
                entry["seconds"] = round(time.perf_counter() - started, 2)
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out.flush()
            print(
                f"{label} b{batches}.{batch} {record['question_id']}: "
                f"{len(entry['claims'])} claims {entry['seconds']}s"
                + (f" ERROR {entry['error'][:80]}" if entry["error"] else ""),
                file=sys.stderr,
            )

        tasks = []
        for record in frozen:
            passages = shown_passages(frozen_evidence(record))
            for label in models:
                for batches in args.batches:
                    for batch, group in enumerate(batched(passages, batches)):
                        if (record["question_id"], label, batches, batch) not in done:
                            tasks.append(call(record, label, batches, batch, group))
        print(f"{len(tasks)} calls ({len(done)} already done)", file=sys.stderr)
        await asyncio.gather(*tasks)
    return 0


# ---------------------------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------------------------

def score(
    frozen: Sequence[dict[str, Any]], calls: Sequence[dict[str, Any]], max_claims: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Per (label, batches) arm: call health, quote fidelity and production acceptance."""
    evidence = {record["question_id"]: frozen_evidence(record) for record in frozen}
    arms: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        if call["question_id"] in evidence:
            arms[(call["label"], call["batches"])].append(call)
    report: dict[str, Any] = {"arms": []}
    misses: list[dict[str, Any]] = []
    for (label, batches), group in sorted(arms.items()):
        categories: Counter[str] = Counter()
        claims = raw_exact = anchored = accepted = 0
        answered: set[str] = set()
        per_question: dict[str, float] = defaultdict(float)
        for call in group:
            per_question[call["question_id"]] = max(
                per_question[call["question_id"]], call.get("seconds") or 0.0,
            )
            shown = evidence[call["question_id"]]
            texts = [text for item in shown if not item.generated for text in exposed_text(item)]
            for data in call["claims"][:max_claims]:
                outcome = judge_claim(Claim.model_validate(data), shown)
                claims += 1
                raw_exact += outcome.raw_exact
                anchored += outcome.anchored and not outcome.raw_exact and outcome.accepted
                accepted += outcome.accepted
                categories[outcome.category] += 1
                if outcome.accepted:
                    answered.add(call["question_id"])
                else:
                    misses.append({
                        "label": label, "batches": batches, "question_id": call["question_id"],
                        "category": outcome.category, "errors": outcome.errors,
                        "quote": data["quote"], "value": data.get("value"),
                        "evidence_ids": data["evidence_ids"],
                        "nearest_source": nearest_window(data["quote"], texts),
                    })
        seconds = [call.get("seconds") or 0.0 for call in group if not call.get("error")]
        outputs = [call["output_tokens"] for call in group if call.get("output_tokens")]
        report["arms"].append({
            "label": label, "batches": batches,
            "questions": len({call["question_id"] for call in group}),
            "calls": len(group),
            "call_errors": sum(bool(call.get("error")) for call in group),
            "parse_errors": sum(bool(call.get("parsing_error")) for call in group),
            "claims": claims,
            "raw_exact_rate": round(raw_exact / claims, 3) if claims else None,
            "accepted_rate": round(accepted / claims, 3) if claims else None,
            "rescued_by_anchoring": anchored,
            "questions_with_accepted_claim": len(answered),
            "categories": dict(categories.most_common()),
            "call_seconds_p50": percentile(seconds, 0.5),
            "call_seconds_p95": percentile(seconds, 0.95),
            "question_seconds_p95": percentile(list(per_question.values()), 0.95),
            "output_tokens_mean": round(statistics.fmean(outputs)) if outputs else None,
        })
    return report, misses


def summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "| Model | Batches | Questions | Calls (errors) | Claims | Exact as returned "
        "| Accepted | Questions answered | Main rejection causes | Call p50 / p95 s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in report["arms"]:
        causes = ", ".join(
            f"{name} {count}" for name, count in arm["categories"].items() if name != "accepted"
        ) or "none"

        def rate(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.0%}"

        lines.append(
            f"| {arm['label']} | {arm['batches']} | {arm['questions']} "
            f"| {arm['calls']} ({arm['call_errors']}) | {arm['claims']} "
            f"| {rate(arm['raw_exact_rate'])} | {rate(arm['accepted_rate'])} "
            f"| {arm['questions_with_accepted_claim']} of {arm['questions']} | {causes} "
            f"| {arm['call_seconds_p50']} / {arm['call_seconds_p95']} |"
        )
    return "\n".join(lines) + "\n"


def _score(args: argparse.Namespace) -> int:
    frozen = read_jsonl(Path(args.frozen))
    calls = [call for path in args.runs for call in read_jsonl(Path(path))]
    report, misses = score(frozen, calls, args.max_claims)
    report["frozen_sha256"] = sha256(Path(args.frozen))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "score.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(summary_markdown(report), encoding="utf-8")
    with (out / "misses.jsonl").open("w", encoding="utf-8") as handle:
        for miss in misses:
            handle.write(json.dumps(miss, ensure_ascii=False) + "\n")
    print(summary_markdown(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crv_agent.evaluation.quote_probe")
    steps = parser.add_subparsers(dest="step", required=True)

    freeze = steps.add_parser("freeze", help="Save the passages production would show")
    freeze.add_argument("--golden", required=True)
    freeze.add_argument("--split", choices=["dev", "heldout"], default="dev")
    freeze.add_argument("--per-category", type=int, default=4)
    freeze.add_argument("--only", action="append", help="Question id (repeatable)")
    freeze.add_argument("--include-abstain", action="store_true")
    freeze.add_argument("--concurrency", type=int, default=2)
    freeze.add_argument("--out", required=True)
    freeze.add_argument("--confirm-live", action="store_true",
                        help="Acknowledge search and query-embedding calls")

    run = steps.add_parser("run", help="Send frozen passages to each model")
    run.add_argument("--frozen", required=True)
    run.add_argument("--model", action="append", required=True,
                     help="Served model name, optionally name@effort (repeatable)")
    run.add_argument("--batches", action="append", type=int,
                     help="Parallel extraction batches per question (repeatable; default 1)")
    run.add_argument("--max-claims", type=int, default=8)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--limit", type=int, default=0, help="First N frozen questions only")
    run.add_argument("--out", required=True, help="JSONL, appended; finished calls are skipped")
    run.add_argument("--confirm-live", action="store_true",
                     help="Acknowledge model calls (CRV passages leave the host if remote)")

    scoring = steps.add_parser("score", help="Offline: validate and classify every claim")
    scoring.add_argument("--frozen", required=True)
    scoring.add_argument("--runs", action="append", required=True)
    scoring.add_argument("--max-claims", type=int, default=8)
    scoring.add_argument("--out-dir", required=True)

    args = parser.parse_args(argv)
    if args.step == "score":
        return _score(args)
    if not args.confirm_live:
        parser.error(f"{args.step} makes live calls; pass --confirm-live")
    if args.step == "run":
        args.batches = sorted(set(args.batches or [1]))
        if any(not 1 <= batches <= 4 for batches in args.batches):
            parser.error("--batches must be 1-4")
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(_freeze(args) if args.step == "freeze" else _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
