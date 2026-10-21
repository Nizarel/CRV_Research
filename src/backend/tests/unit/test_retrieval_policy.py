import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from azure.search.documents.aio import SearchClient
from langgraph.checkpoint.memory import InMemorySaver

from crv_agent.adapters.search import AzureSearch
from crv_agent.domain.evidence import Evidence, WorkerResult
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.domain.planning import DocumentPage, Entity, Facet, QueryPlan
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.graph import build_graph
from crv_agent.services.retrieval import Retrieval, focus
from crv_agent.services.verification import claim_errors
from crv_agent.tools.evidence import evidence_tools
from tests.fakes import CLAIM, SOURCE, FakeKnowledge, FakeModels, FakeWorker


def candidates() -> list[Evidence]:
    return [
        SOURCE.model_copy(update={
            "id": f"S{i}", "chunk_id": f"c{i}", "content_hash": f"{i:064x}",
        }) for i in range(50)
    ]


class Search:
    def __init__(self, hits: list[Evidence] | None = None) -> None:
        self.hits = candidates() if hits is None else hits
        self.calls: list[tuple[str, str, str, list[str], int]] = []

    async def query(
        self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
    ) -> list[Evidence]:
        self.calls.append((text, release, mode, hashes, top))
        return [] if hashes else self.hits[:top]

    async def ready(self) -> None:
        return None


@pytest.mark.asyncio
async def test_azure_adapter_requests_fifty_semantic_hybrid_candidates() -> None:
    async def empty_rows():
        if False:
            yield {}

    client = AsyncMock(spec=SearchClient)
    client.search.return_value = empty_rows()
    converted_pdf_hash = "f" * 64
    assert await AzureSearch(client, "r1").query(
        "Recovery?", "r1", "semantic", [converted_pdf_hash],
    ) == []
    options = client.search.call_args.kwargs
    assert options["top"] == 50
    assert options["query_type"] == "semantic"
    assert options["semantic_configuration_name"] == "crv-evidence-semantic"
    assert options["vector_queries"][0].k_nearest_neighbors == 50
    assert converted_pdf_hash in options["filter"]


@pytest.mark.asyncio
async def test_depth_fifty_semantic_fallback_and_exact_converted_hash() -> None:
    search, budget = Search(), Budget()
    converted_pdf_hash = "f" * 64
    hits = await Retrieval(search).retrieve("Recovery?", "r1", [converted_pdf_hash], budget)
    assert len(hits) == 50
    assert hits[-1].id == "S49"
    assert search.calls == [
        ("Recovery?", "r1", "semantic", [], 50),
        ("Recovery?", "r1", "semantic", [converted_pdf_hash], 50),
    ]
    assert budget.retrieval_calls == 2


def test_focused_ranking_uses_measured_soft_penalties_and_keeps_every_hit() -> None:
    hits = candidates()
    hits[:3] = [hit.model_copy(update={"content_hash": "a" * 64}) for hit in hits[:3]]
    hits[3] = hits[3].model_copy(update={
        "original_sources": ["raw/Rapport%20d%27activit%C3%A9%20mensuel.pdf"],
    })
    result = focus(hits)
    positions = {hit.id: i for i, hit in enumerate(result)}
    assert len(result) == 50
    assert {hit.id for hit in result} == {hit.id for hit in hits}
    assert positions["S21"] < positions["S2"] < positions["S22"]
    assert positions["S12"] < positions["S3"] < positions["S13"]
    secondary = next(hit for hit in result if hit.id == "S3")
    assert secondary.source_role == "secondary_summary"
    assert secondary.source_role_basis == "filename"
    assert result[0].source_role == "unclassified"


@pytest.mark.parametrize("filename", [
    "RA septembre.pdf", "Monthly report.pdf", "Executive summary.pdf",
    "Flash hebdomadaire.pdf", "Reporting projet.pdf",
])
def test_periodic_source_roles(filename: str) -> None:
    result = focus([SOURCE.model_copy(update={"source_uri": filename})])
    assert result[0].source_role == "secondary_summary"


@pytest.mark.asyncio
async def test_worker_can_read_rank_fifty_without_grounding_in_unseen_text() -> None:
    hits = candidates()
    long_text = "x" * 3000 + SOURCE.content + "y" * 8000 + "UNREAD quotation."
    hits[0] = hits[0].model_copy(update={"content": long_text})
    hits[-1] = hits[-1].model_copy(update={"content": long_text})
    search, budget = Search(hits), Budget()
    found: dict[str, Evidence] = {}
    search_tool, read_tool = evidence_tools(
        Retrieval(search), "r1", [], budget, found, "CR-AR-PE-01-15 recovery?",
    )
    listing = json.loads(await search_tool.ainvoke({"query": "Rewritten question"}))
    assert len(listing["candidates"]) == 50
    assert len(listing["evidence"]) == 8
    assert listing["evidence"][0]["truncated"]
    assert "CR-AR-PE-01-15" in search.calls[0][0]
    assert search.calls[0][2] == "semantic"
    assert search.calls[1][2] == "lexical"
    assert "S49" not in found
    first_claim = CLAIM.model_copy(update={"evidence_ids": ["S0"]})
    assert claim_errors(first_claim, found)
    read = json.loads(await read_tool.ainvoke({"evidence_ids": ["S49", "S0"]}))
    assert read[0]["truncated"] and len(read[0]["content"]) == 10000
    assert not claim_errors(CLAIM.model_copy(update={"evidence_ids": ["S49"]}), found)
    assert not claim_errors(first_claim, found)
    assert claim_errors(CLAIM.model_copy(update={
        "evidence_ids": ["S49"], "quote": "UNREAD quotation.", "value": None,
    }), found)
    assert budget.retrieval_calls == 2  # Reads use the cache, not another Search request.


@pytest.mark.asyncio
async def test_read_and_rewrite_limits_remain_bounded() -> None:
    found: dict[str, Evidence] = {}
    search, read = evidence_tools(Retrieval(Search()), "r1", [], Budget(), found, "Recovery?")
    with pytest.raises(ValueError, match="IDs returned"):
        await read.ainvoke({"evidence_ids": ["invented"]})
    for _ in range(3):
        await search.ainvoke({"query": "Recovery?"})
    with pytest.raises(BudgetExceeded, match="rewrite"):
        await search.ainvoke({"query": "Recovery?"})
    with pytest.raises(ValueError, match="one and four"):
        await read.ainvoke({"evidence_ids": ["S0"] * 5})
    for _ in range(3):
        await read.ainvoke({"evidence_ids": ["S49"]})
    with pytest.raises(BudgetExceeded, match="evidence-read"):
        await read.ainvoke({"evidence_ids": ["S49"]})


@pytest.mark.asyncio
async def test_parallel_retrieval_reserves_the_whole_budget_before_search() -> None:
    search = Search()
    with pytest.raises(BudgetExceeded):
        await Retrieval(search).retrieve("Recovery?", "r1", ["a" * 64], Budget(retrieval_limit=1))
    assert not search.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["lookup", "synthesis", "catalogue"])
async def test_precise_steering_preserves_broad_catalogue_navigation(intent: str) -> None:
    entities = [
        Entity(iri=kind + status, name=kind, kind=kind, status=status)
        for kind, status in [
            ("Project", "approved"), ("Organisation", "candidate"), ("Organisation", "observed"),
            ("Organisation", "approved"), ("Site", "candidate"), ("ReportCode", "observed"),
            ("Site", "rejected"),
        ]
    ]

    class Knowledge(FakeKnowledge):
        selections: list[list[str]] = []

        async def resolve(self, text: str, release: str) -> list[Entity]:
            return entities

        async def documents(
            self, selected: list[str], release: str, cursor: str | None,
        ) -> DocumentPage:
            self.selections.append(selected)
            return await super().documents(selected, release, cursor)

    knowledge = Knowledge()
    graph = build_graph(InMemorySaver())
    await graph.ainvoke(
        {"question": "Recovery?", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=FakeModels(intent), knowledge=knowledge, worker=FakeWorker(),
            budget=Budget(), search_release="r1", kg_release="kg1", trace_id="test",
        ),
    )
    expected = [e.iri for e in entities] if intent == "catalogue" else [
        "Organisationapproved", "Sitecandidate", "ReportCodeobserved",
    ]
    assert knowledge.selections == [expected]


@pytest.mark.asyncio
async def test_no_precise_entities_never_enumerates_whole_corpus_as_a_boost() -> None:
    longer_read_ready = asyncio.Event()
    long_content = "prefix " * 1000 + SOURCE.content

    class Knowledge(FakeKnowledge):
        async def resolve(self, text: str, release: str) -> list[Entity]:
            return [Entity(iri="project", name="Project", kind="Project", status="approved")]

        async def documents(
            self, entities: list[str], release: str, cursor: str | None,
        ) -> DocumentPage:
            raise AssertionError("Empty steering must not enumerate the whole corpus")

    class Worker(FakeWorker):
        questions: list[str] = []

        async def run(self, question: str, release: str, hashes: list[str], budget: Budget):
            assert hashes == []
            self.questions.append(question)
            await budget.reserve(retrieval=1)
            if question.startswith("Pilot"):
                longer_read_ready.set()
                return WorkerResult(claims=[CLAIM], gaps=[]), [
                    SOURCE.model_copy(update={"content": long_content}),
                ]
            await longer_read_ready.wait()
            return WorkerResult(claims=[], gaps=[]), [
                SOURCE.model_copy(update={"content": long_content[:2500]}),
            ]

    class Models(FakeModels):
        async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
            result = await super().plan(question, history, budget)
            return result.model_copy(update={"facets": [
                Facet(question="Pilot recovery"), Facet(question="Lab recovery"),
            ]})

    worker = Worker()
    result = await build_graph(InMemorySaver()).ainvoke(
        {"question": "Compare pilot and lab recovery", "history": []},
        {"configurable": {"thread_id": str(uuid4())}},
        context=ResearchContext(
            models=Models("comparison"), knowledge=Knowledge(), worker=worker, budget=Budget(),
            search_release="r1", kg_release="kg1", trace_id="test",
        ),
    )
    assert len(worker.questions) == 2
    assert {question.splitlines()[0] for question in worker.questions} == {
        "Pilot recovery", "Lab recovery",
    }
    assert result["evidence"][0].content == long_content
    assert result["answer"].status in {"answered", "partial"}
