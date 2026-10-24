import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from azure.search.documents.aio import SearchClient
from langgraph.checkpoint.memory import InMemorySaver

from crv_agent.adapters.postgres.memory import serializer
from crv_agent.adapters.search import AzureSearch
from crv_agent.domain.evidence import Evidence, TracedWorkerResult, WorkerResult
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.domain.planning import DocumentPage, Entity, Facet, QueryPlan
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.graph import build_graph
from crv_agent.services.evidence_pack import (
    EvidencePolicy,
    best_window,
    expose,
    exposed_text,
    merge_exposure,
    term_weights,
)
from crv_agent.services.retrieval import Retrieval, focus, identifiers
from crv_agent.services.verification import claim_errors
from crv_agent.tools.evidence import evidence_tools
from tests.fakes import CLAIM, SOURCE, FakeKnowledge, FakeModels, FakeWorker


def candidates() -> list[Evidence]:
    return [
        SOURCE.model_copy(update={
            "id": f"S{i}", "chunk_id": f"c{i}", "content_hash": f"{i:064x}",
        }) for i in range(50)
    ]


def test_document_identifiers_exclude_hyphenated_question_words() -> None:
    assert identifiers("Comment le probleme a-t-il evolue ?") == []
    assert identifiers("Qu'a-t-on teste dans CR-AR-CO-RP-19-10 et KM5962 ?") == [
        "CR-AR-CO-RP-19-10", "KM5962",
    ]
    assert identifiers("CR/AF/AG/RP/10/15 et CR/AF/AG/RP/10/15") == ["CR/AF/AG/RP/10/15"]


class Search:
    def __init__(self, hits: list[Evidence] | None = None) -> None:
        self.hits = candidates() if hits is None else hits
        self.calls: list[tuple[str, str, str, list[str], int]] = []

    async def query(
        self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        captions: bool = False,
    ) -> list[Evidence]:
        self.calls.append((text, release, mode, hashes, top))
        return [] if hashes else self.hits[:top]

    async def ready(self) -> None:
        return None

    async def tables(self, release: str, pages: dict[str, list[int]]) -> list[Evidence]:
        return []


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


@pytest.mark.asyncio
async def test_strict_scope_searches_only_the_given_documents() -> None:
    search, budget = Search(), Budget()
    chosen = ["a" * 64, "b" * 64]
    await Retrieval(search).retrieve("Recovery 2015?", "r1", chosen, budget, strict=True)
    assert search.calls == [
        ("Recovery 2015?", "r1", "semantic", chosen, 50),
        ("Recovery 2015?", "r1", "lexical", chosen, 50),
    ]
    assert budget.retrieval_calls == 2
    search = Search()
    await Retrieval(search).retrieve("Recovery?", "r1", [], Budget(), strict=True)
    assert search.calls == [("Recovery?", "r1", "semantic", [], 50)]


@pytest.mark.asyncio
async def test_referenced_table_is_exposed_without_another_model_search() -> None:
    hits = candidates()
    hits[4] = hits[4].model_copy(update={
        "content": "Scenarios de flottation (voir tableau No 2).", "pages": [4],
    })
    table = SOURCE.model_copy(update={
        "id": "table-p5", "chunk_id": "table-p5", "content_hash": hits[4].content_hash,
        "pages": [5], "content": "| Scenario 5 | CR global (%) |\n| 5 | 24,20 |",
    })
    scopes: list[dict[str, list[int]]] = []

    class Tables(Search):
        async def tables(self, release: str, pages: dict[str, list[int]]) -> list[Evidence]:
            assert release == "r1"
            scopes.append(pages)
            return [
                table, table.model_copy(update={"id": "other-doc", "content_hash": "f" * 64}),
                table.model_copy(update={"id": "other-release", "release": "r2"}),
                table.model_copy(update={"id": "far-page", "pages": [9]}),
                table.model_copy(update={"id": "generated", "generated": True}),
            ]

    search, budget = Tables(hits), Budget()
    result = await Retrieval(search).retrieve("Scenarios de flottation?", "r1", [], budget)
    assert len(result) == 50
    assert result[5] == table
    assert scopes == [{hits[4].content_hash: [3, 4, 5]}]
    assert len(search.calls) == 1
    assert budget.retrieval_calls == 2
    assert not {"other-doc", "other-release", "far-page", "generated"} & {hit.id for hit in result}
    scopes.clear()
    result = await Retrieval(search).retrieve(
        "Scenarios?", "r1", [], Budget(retrieval_limit=1),
    )
    assert result == focus(hits)
    assert scopes == []


@pytest.mark.asyncio
async def test_table_lookup_is_filtered_and_never_vectorizes() -> None:
    async def empty_rows():
        if False:
            yield {}

    client = AsyncMock(spec=SearchClient)
    client.search.return_value = empty_rows()
    search = AzureSearch(client, "r1")
    assert await search.tables("r1", {"a" * 64: [4, 5]}) == []
    options = client.search.call_args.kwargs
    assert options["search_text"] == "*"
    assert options["query_type"] == "simple"
    assert options["top"] == 100
    assert "vector_queries" not in options
    assert "semantic_configuration_name" not in options
    assert "chunk_kind eq 'table'" in options["filter"]
    assert "is_generated eq false" in options["filter"]
    assert "release_id eq 'r1'" in options["filter"]
    assert "page eq 4 or page eq 5" in options["filter"]
    assert "a" * 64 in options["filter"]
    client.search.reset_mock()
    assert await search.tables("r1", {}) == []
    client.search.assert_not_called()
    with pytest.raises(ValueError, match="scope"):
        await search.tables("r1", {"a" * 64: [-1]})


@pytest.mark.asyncio
async def test_neighboring_tables_are_bounded_and_failure_keeps_ranked_hits() -> None:
    hits = [hit.model_copy(update={
        "content": "Results are in Table 2.", "pages": [4],
    }) for hit in candidates()]
    scopes: list[dict[str, list[int]]] = []

    class Tables(Search):
        fail = False

        async def tables(self, release: str, pages: dict[str, list[int]]) -> list[Evidence]:
            scopes.append(pages)
            if self.fail:
                raise ConnectionError("Search unavailable")
            return [SOURCE.model_copy(update={
                "id": f"{digest}-{number}", "chunk_id": f"{digest}-{number}",
                "content_hash": digest, "pages": [5],
            }) for digest in pages for number in range(3)]

    search = Tables(hits)
    result = await Retrieval(search).with_tables(hits, "r1", Budget())
    assert len(scopes[0]) == 5
    assert len(result) == 50
    assert len({hit.id for hit in result} - {hit.id for hit in hits}) == 8
    search.fail = True
    assert await Retrieval(search).with_tables(hits, "r1", Budget()) == hits


def test_focused_ranking_uses_measured_soft_penalties_and_keeps_every_hit() -> None:
    hits = candidates()
    hits[:3] = [hit.model_copy(update={"content_hash": "a" * 64}) for hit in hits[:3]]
    hits[3] = hits[3].model_copy(update={
        "source_path": "Documents Activité/Rapport d'activité mensuel.pdf",
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
    result = focus([SOURCE.model_copy(update={"source_path": "GED/" + filename})])
    assert result[0].source_role == "secondary_summary"


@pytest.mark.asyncio
async def test_pack_focuses_excerpts_and_never_grounds_in_unseen_text() -> None:
    filler = "Contexte général du rapport sans résultat chiffré. " * 120
    long_text = filler + SOURCE.content + " " + filler + "UNREAD quotation."
    hits = [hit.model_copy(update={"content": long_text}) for hit in candidates()]
    hits[30] = hits[30].model_copy(update={"title": "CR-AR-PE-01-15 final report"})
    search, budget = Search(hits), Budget()
    found: dict[str, Evidence] = {}
    search_tool, read_tool = evidence_tools(
        Retrieval(search), "r1", [], budget, found, "CR-AR-PE-01-15 recovery?",
        EvidencePolicy(pack_size=16, excerpt_characters=1200),
    )
    listing = json.loads(await search_tool.ainvoke({"query": "pilot recovery"}))
    assert len(listing["evidence"]) == 16
    assert len(listing["more_candidates"]) == 34
    assert listing["evidence"][0]["id"] == "S30"  # Exact report-code match is promoted.
    assert all(SOURCE.content in item["text"] for item in listing["evidence"])
    assert all(item["truncated"] and len(item["text"]) <= 1200 for item in listing["evidence"])
    assert "CR-AR-PE-01-15" in search.calls[0][0]
    assert [call[2] for call in search.calls] == ["semantic", "lexical"]
    assert "S49" not in found
    deep = CLAIM.model_copy(update={"evidence_ids": ["S49"]})
    assert claim_errors(deep, found)
    assert not claim_errors(CLAIM.model_copy(update={"evidence_ids": ["S0"]}), found)
    read = json.loads(await read_tool.ainvoke({"evidence_ids": ["S49", "S0"]}))
    assert read[0]["truncated"] and len(read[0]["text"]) <= 8000
    assert not claim_errors(deep, found)
    assert claim_errors(CLAIM.model_copy(update={
        "evidence_ids": ["S49"], "quote": "UNREAD quotation.", "value": None,
    }), found)
    assert found["S49"].content == long_text  # Citations retain the complete source chunk.
    assert budget.retrieval_calls == 2  # Reads use the cache, not another Search request.


def test_windows_snap_to_words_and_exposure_merges_only_shown_spans() -> None:
    content = "alpha " * 400 + "recovery 87.5 % pilot " + "omega " * 400
    weights = term_weights("pilot recovery", [SOURCE.model_copy(update={
        "content": content,
    })], [])
    start, end = best_window(content, weights, 200)
    assert "recovery 87.5 % pilot" in content[start:end]
    assert content[start - 1] == " " and content[end] == " "
    left = expose(SOURCE.model_copy(update={"content": content}), (0, 10))
    right = expose(SOURCE.model_copy(update={"content": content}), (5, 20))
    other = expose(SOURCE.model_copy(update={"content": content}), (100, 110))
    assert merge_exposure(merge_exposure(left, right), other).exposed == [(0, 20), (100, 110)]
    assert exposed_text(other) == [content[100:110]]
    with pytest.raises(ValueError):
        merge_exposure(left, SOURCE)


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
async def test_extraction_claim_cap_is_configurable_and_enforced() -> None:
    from crv_agent.orchestration.extraction import Extraction, ExtractionWorker

    class Model:
        def __init__(self) -> None:
            self.data: dict[str, object] = {}

        def with_structured_output(self, *args: object, **kwargs: object) -> "Model":
            return self

        async def ainvoke(self, messages: list[object], config: object = None) -> Extraction:
            self.data = json.loads(messages[1].content)  # type: ignore[attr-defined]
            return Extraction(claims=[
                CLAIM.model_copy(update={"text": f"Recovery claim {n}"}) for n in range(5)
            ], gaps=[], rewrite=None)

    model = Model()
    worker = ExtractionWorker(
        model, Retrieval(Search()), 1024, max_claims=3,  # type: ignore[arg-type]
    )
    result, _ = await worker.run("Recovery?", "r1", [], Budget())
    assert model.data["max_claims"] == 3
    assert len(result.claims) == 3
    assert isinstance(result, TracedWorkerResult)
    [traced] = result.rounds
    assert (len(traced.returned), len(traced.capped)) == (3, 2)
    assert traced.passage_ids == [p["id"] for p in model.data["passages"]]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batched_extraction_splits_passages_and_merges_claims() -> None:
    from crv_agent.orchestration.extraction import Extraction, ExtractionWorker

    class Model:
        def __init__(self) -> None:
            self.batches: list[list[str]] = []

        def with_structured_output(self, *args: object, **kwargs: object) -> "Model":
            return self

        async def ainvoke(self, messages: list[object], config: object = None) -> Extraction:
            data = json.loads(messages[1].content)  # type: ignore[attr-defined]
            self.batches.append([p["id"] for p in data["passages"]])
            return Extraction(claims=[CLAIM], gaps=["Batch lacks the 2019 value"], rewrite=None)

    model = Model()
    worker = ExtractionWorker(
        model, Retrieval(Search()), 1024, batches=2,  # type: ignore[arg-type]
    )
    result, shown = await worker.run("Recovery?", "r1", [], Budget())
    first, second = model.batches
    assert not set(first) & set(second)
    assert sorted(first + second) == sorted(e.id for e in shown)
    assert result.claims == [CLAIM]  # duplicates across batches are merged
    assert not any("2019" in gap for gap in result.gaps)


@pytest.mark.asyncio
async def test_failed_extraction_batch_keeps_the_other_batches_claims() -> None:
    from crv_agent.orchestration.extraction import Extraction, ExtractionWorker

    class Model:
        calls = 0
        fail_all = False

        def with_structured_output(self, *args: object, **kwargs: object) -> "Model":
            return self

        async def ainvoke(self, messages: list[object], config: object = None) -> Extraction:
            Model.calls += 1
            if Model.fail_all or Model.calls == 1:
                raise BudgetExceeded("model-call")
            await asyncio.sleep(0)
            return Extraction(claims=[CLAIM], gaps=[], rewrite=None)

    worker = ExtractionWorker(
        Model(), Retrieval(Search()), 1024, batches=2,  # type: ignore[arg-type]
    )
    result, _ = await worker.run("Recovery?", "r1", [], Budget())
    assert result.claims == [CLAIM]
    assert any("batch failed" in gap for gap in result.gaps)

    Model.fail_all = True  # every batch fails: the original exception type reaches the facet
    with pytest.raises(BudgetExceeded):
        await worker.run("Recovery?", "r1", [], Budget())


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

        async def run(
            self, question: str, release: str, hashes: list[str], budget: Budget,
            *, strict: bool = False,
        ):
            assert hashes == []
            self.questions.append(question)
            await budget.reserve(retrieval=1)
            fact = len(long_content) - len(SOURCE.content)
            full = SOURCE.model_copy(update={"content": long_content})
            if question.startswith("Pilot"):
                longer_read_ready.set()
                return WorkerResult(claims=[CLAIM], gaps=[]), [
                    expose(full, (fact, len(long_content))),
                ]
            await longer_read_ready.wait()
            return WorkerResult(claims=[], gaps=[]), [expose(full, (0, 2500))]

    class Models(FakeModels):
        async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
            result = await super().plan(question, history, budget)
            return result.model_copy(update={"facets": [
                Facet(question="Pilot recovery"), Facet(question="Lab recovery"),
            ]})

    worker = Worker()
    result = await build_graph(InMemorySaver(serde=serializer())).ainvoke(
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
    assert result["evidence"][0].exposed == [
        (0, 2500), (len(long_content) - len(SOURCE.content), len(long_content)),
    ]
    assert result["answer"].status in {"answered", "partial"}
    assert result["answer"].citations[0].exposed == result["evidence"][0].exposed


@pytest.mark.asyncio
async def test_default_policy_exposes_all_fifty_full_chunks() -> None:
    long_text = "contexte " * 400 + SOURCE.content
    hits = [hit.model_copy(update={"content": long_text}) for hit in candidates()]
    found: dict[str, Evidence] = {}
    search_tool, _ = evidence_tools(Retrieval(Search(hits)), "r1", [], Budget(), found, "Q?")
    listing = json.loads(await search_tool.ainvoke({"query": "recovery"}))
    assert len(listing["evidence"]) == 50 and listing["more_candidates"] == []
    assert all(not item["truncated"] and item["text"] == long_text for item in listing["evidence"])
    assert not claim_errors(CLAIM.model_copy(update={"evidence_ids": ["S49"]}), found)
