"""Retrieval-depth experiment: emit the exact context each arm would expose (no judging).

Each output line is one (question, arm) record with the passages a worker would see, plus
cost, latency and composition metadata. Scoring is done by the shared golden harness
(`python -m ingest_pipeline.tools.golden score`), so numbers stay comparable with
`golden-eval`. Parity arms (A0/A1) reproduce golden-eval's semantic modes exactly.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget
from crv_agent.evaluation.dataset import GoldenQuestion, load_questions
from crv_agent.ports.knowledge import Knowledge
from crv_agent.ports.models import Models
from crv_agent.ports.rerank import Reranker
from crv_agent.ports.retrieval import Search
from crv_agent.services.candidates import ranked_candidates
from crv_agent.services.catalogue import enumerate_documents
from crv_agent.services.evidence_pack import EvidencePolicy, build_pack
from crv_agent.services.navigation import steering_entities
from crv_agent.services.retrieval import Retrieval, focus, identifiers

PassageMode = Literal["truncate", "split"]
BEGIN, END = "=== AGENT CONTEXTS BEGIN ===", "=== AGENT CONTEXTS END ==="

P16 = EvidencePolicy(pack_size=16, excerpt_characters=1200)
POLICIES: dict[str, EvidencePolicy] = {
    "P16": P16,
    "P12": EvidencePolicy(pack_size=12, excerpt_characters=1200),
    "P24": EvidencePolicy(pack_size=24, excerpt_characters=1200),
    "P16W2000": EvidencePolicy(pack_size=16, excerpt_characters=2000),
    "CAP": EvidencePolicy(pack_size=16, excerpt_characters=1200, anchor="caption"),
    "LLM": EvidencePolicy(pack_size=16, excerpt_characters=1200, rerank="llm"),
    "FULL50": EvidencePolicy(),
}
ARMS = ("A0", "A1", *POLICIES, "DEC", "KGB", "CEIL", "AAR")


@dataclass
class Context:
    id: str
    content_hash: str
    title: str
    source: str
    pages: list[int]
    text: str
    source_role: str = "unclassified"


@dataclass
class ArmOutput:
    contexts: list[Context]
    passage_mode: PassageMode
    notes: dict[str, Any] = field(default_factory=dict)


def full(hits: Sequence[Evidence]) -> list[Context]:
    return [
        Context(
            h.id, h.content_hash, h.title, h.source_path or h.source_uri, h.pages, h.content,
            h.source_role,
        )
        for h in hits
    ]


def shown(spans: dict[str, tuple[Evidence, list[tuple[int, int]]]]) -> list[Context]:
    """Exposed spans, merged per evidence (overlapping excerpts from several facets)."""
    contexts = []
    for hit, ranges in spans.values():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for index, (start, end) in enumerate(merged):
            contexts.append(Context(
                f"{hit.id}#{index}" if len(merged) > 1 else hit.id, hit.content_hash,
                hit.title, hit.source_path or hit.source_uri, hit.pages, hit.content[start:end],
                hit.source_role,
            ))
    return contexts


class Arms:
    def __init__(
        self, search: Search, knowledge: Knowledge | None, models: Models | None,
        reranker: Reranker | None, release: str, kg_release: str,
        knowledge_base: Callable[[str], Awaitable[list[Context]]] | None = None,
    ) -> None:
        self.search, self.knowledge, self.models = search, knowledge, models
        self.retrieval, self.reranker = Retrieval(search), reranker
        self.release, self.kg_release = release, kg_release
        self.knowledge_base = knowledge_base

    async def pack(
        self, queries: list[str], question: str, policy: EvidencePolicy, hashes: list[str],
        budget: Budget,
    ) -> list[Context]:
        fixed = identifiers(question)
        spans: dict[str, tuple[Evidence, list[tuple[int, int]]]] = {}
        for query in queries:
            text = (query + "\n" + " ".join(fixed)).strip()
            hits = await ranked_candidates(
                self.retrieval, text, self.release, hashes, budget, policy, self.reranker,
            )
            for hit, span in build_pack(hits, text, fixed, policy).shown:
                spans.setdefault(hit.id, (hit, []))[1].append(span)
        return shown(spans)

    async def facets(self, question: str, budget: Budget) -> tuple[list[str], str]:
        if self.models is None:
            raise ValueError("DEC/KGB need the planner model")
        plan = await self.models.plan(question, [], budget)
        return [f.question for f in plan.facets] or [question], plan.intent

    async def run(self, arm: str, question: str, budget: Budget) -> ArmOutput:
        if arm == "A0":
            await budget.reserve(retrieval=1)
            hits = await self.search.query(question, self.release, "semantic", [], top=10)
            return ArmOutput(full(hits), "truncate")
        if arm == "A1":
            await budget.reserve(retrieval=1)
            hits = await self.search.query(question, self.release, "semantic", [], top=50)
            return ArmOutput(full(focus(hits)[:10]), "truncate")
        if arm in POLICIES:
            return ArmOutput(
                await self.pack([question], question, POLICIES[arm], [], budget), "split",
            )
        if arm == "CEIL":
            text = (question + "\n" + " ".join(identifiers(question))).strip()
            hits = await self.retrieval.retrieve(text, self.release, [], budget)
            return ArmOutput(full(hits), "split")
        if arm in ("DEC", "KGB"):
            queries, intent = await self.facets(question, budget)
            notes: dict[str, Any] = {"facets": queries, "intent": intent}
            hashes: list[str] = []
            if arm == "KGB":
                if self.knowledge is None:
                    raise ValueError("KGB needs the KG read API")
                await budget.reserve(retrieval=1)
                entities = await self.knowledge.resolve(question, self.kg_release)
                selected = steering_entities(entities, intent)
                notes["entities"] = [f"{e.kind}:{e.status}:{e.name}" for e in selected]
                if selected:
                    documents, _ = await enumerate_documents(
                        self.knowledge, [e.iri for e in selected], self.kg_release, budget,
                    )
                    hashes = sorted({d.content_hash for d in documents if d.content_hash})
                notes["kg_documents"] = len(hashes)
            contexts = await self.pack(queries, question, P16, hashes, budget)
            return ArmOutput(contexts, "split", notes)
        if arm == "AAR":
            if self.knowledge_base is None:
                raise ValueError("AAR needs --knowledge-base")
            await budget.reserve(retrieval=1)
            return ArmOutput(await self.knowledge_base(question), "split")
        raise ValueError(f"Unknown arm {arm}")


def record(
    question: GoldenQuestion, arm: str, output: ArmOutput | None, budget: Budget,
    seconds: float, error: str | None,
) -> dict[str, Any]:
    contexts = output.contexts if output else []
    sources = [c.source for c in contexts]
    return {
        "id": question.id, "category": question.category, "split": question.split,
        "arm": arm, "passage_mode": output.passage_mode if output else "split",
        "error": error, "seconds": round(seconds, 3), "usage": budget.usage(),
        "exposed_characters": sum(len(c.text) for c in contexts),
        "documents": len({c.content_hash for c in contexts}),
        "secondary_share": round(
            sum(c.source_role == "secondary_summary" for c in contexts) / len(contexts), 3,
        ) if contexts else None,
        "dossier_cu_share": round(
            sum("dossier cu" in s.casefold() for s in sources) / len(sources), 3,
        ) if sources else None,
        "notes": output.notes if output else {},
        "contexts": [c.__dict__ for c in contexts],
    }


async def evaluate(
    questions: Sequence[GoldenQuestion], arms: Sequence[str], runner: Arms, concurrency: int,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    gate = asyncio.Semaphore(concurrency)

    async def one(question: GoldenQuestion) -> None:
        async with gate:
            for arm in arms:
                budget = Budget(retrieval_limit=200, model_limit=40, token_limit=2_000_000,
                                seconds=600)
                started = time.perf_counter()
                output, error = None, None
                try:
                    output = await runner.run(arm, question.question, budget)
                except Exception as exc:  # recorded per arm; never silently dropped
                    error = f"{type(exc).__name__}: {exc}"[:500]
                emit(record(question, arm, output, budget, time.perf_counter() - started, error))

    await asyncio.gather(*(one(q) for q in questions))


async def _run(args: argparse.Namespace) -> int:
    from azure.identity.aio import DefaultAzureCredential

    from crv_agent.adapters.models import LangChainModels, LangChainReranker
    from crv_agent.runtime.infrastructure import infrastructure
    from crv_agent.runtime.settings import Settings

    settings = Settings()
    questions = load_questions(Path(args.golden), args.split)
    if args.only:
        questions = [q for q in questions if q.id in set(args.only)]
    arms = args.arm or list(ARMS)
    if "AAR" in arms and not args.knowledge_base:
        arms = [arm for arm in arms if arm != "AAR"]
    out = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")  # noqa: SIM115
    try:
        async with AsyncExitStack() as stack:
            infra = await stack.enter_async_context(infrastructure(settings, memory=False))
            models = LangChainModels(
                infra.models["planner"], infra.models["answer"], infra.models["verifier"],
                settings.output_tokens,
            )
            knowledge_base = None
            if args.knowledge_base:
                credential = await stack.enter_async_context(DefaultAzureCredential())
                knowledge_base = await stack.enter_async_context(_knowledge_base(
                    settings.search_endpoint, args.knowledge_base, args.knowledge_source,
                    settings.search_release, credential,
                ))
            runner = Arms(
                infra.search, infra.knowledge, models,
                LangChainReranker(models, infra.models["worker"]),
                settings.search_release, settings.kg_release, knowledge_base,
            )
            if out is sys.stdout:
                print(BEGIN, flush=True)

            def emit(item: dict[str, Any]) -> None:
                out.write(json.dumps(item, ensure_ascii=False) + "\n")
                out.flush()

            await evaluate(questions, arms, runner, args.concurrency, emit)
            if out is sys.stdout:
                print(END, flush=True)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


@asynccontextmanager
async def _knowledge_base(
    endpoint: str, name: str, source: str, release: str, credential: Any,
) -> AsyncIterator[Callable[[str], Awaitable[list[Context]]]]:
    from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient

    release_filter = "release_id eq '" + release.replace("'", "''") + "'"
    async with KnowledgeBaseRetrievalClient(
        endpoint, credential, knowledge_base_name=name,
    ) as client:

        async def retrieve(question: str) -> list[Context]:
            response = await client.retrieve({
                "intents": [{"type": "semantic", "search": question}],
                "knowledgeSourceParams": [{
                    "kind": "searchIndex", "knowledgeSourceName": source,
                    "filterAddOn": release_filter,
                    "includeReferences": True, "includeReferenceSourceData": True,
                }],
            })
            contexts = []
            for reference in response.get("references") or []:
                data = reference.get("sourceData") or {}
                if data.get("release_id") not in (None, release):
                    raise ValueError("Knowledge base returned another release")
                contexts.append(Context(
                    str(data.get("projection_id") or reference.get("docKey")),
                    str(data.get("content_hash") or ""), str(data.get("title") or ""),
                    str(data.get("source_path") or data.get("source_uri") or ""),
                    list(data.get("original_page_numbers") or []),
                    str(data.get("content") or ""),
                ))
            return contexts

        yield retrieve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crv_agent.evaluation.retrieval")
    parser.add_argument("--golden", required=True)
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    parser.add_argument("--allow-heldout", action="store_true",
                        help="Required for --split heldout (run once, for the frozen winner)")
    parser.add_argument("--arm", action="append", choices=ARMS)
    parser.add_argument("--only", action="append", help="Question id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--knowledge-base", default="", help="AAR knowledge base name")
    parser.add_argument("--knowledge-source", default="crv-evidence-ks")
    parser.add_argument("--out", default="-", help="JSONL path, or - for stdout with markers")
    parser.add_argument("--confirm-live", action="store_true",
                        help="Acknowledge paid Search/model calls on Azure")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("Live Azure retrieval requires --confirm-live")
    if args.split == "heldout" and not args.allow_heldout:
        parser.error("The held-out split requires --allow-heldout")
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
