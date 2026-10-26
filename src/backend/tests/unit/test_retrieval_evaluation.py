import pytest

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import DocumentPage, Entity, Facet, QueryPlan
from crv_agent.evaluation import retrieval as evaluation
from crv_agent.evaluation.dataset import GoldenQuestion
from crv_agent.evaluation.retrieval import ARMS, Arms, Context, evaluate
from tests.fakes import SOURCE, FakeKnowledge, FakeModels

LONG = "contexte " * 400 + "Recovery 87.5 % at pilot scale. " + "suite " * 400


def hits(n: int = 50) -> list[Evidence]:
    return [
        SOURCE.model_copy(update={
            "id": f"S{i}", "chunk_id": f"c{i}", "content_hash": f"{i:064x}", "content": LONG,
            "source_path": "Geometallurgy Cu/Dossier Cu/report.pdf" if i % 2 else "RA mai.pdf",
        }) for i in range(n)
    ]


class Search:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str], int]] = []

    async def query(
        self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        captions: bool = False,
    ) -> list[Evidence]:
        self.calls.append((text, mode, hashes, top))
        return hits()[:top]

    async def ready(self) -> None:
        return None


class Planner(FakeModels):
    async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
        await budget.reserve(model=1, tokens=10)
        return QueryPlan(
            intent="comparison", language="en",
            facets=[Facet(question="pilot recovery"), Facet(question="lab recovery")],
            identifiers=[], premises=[], clarification=None,
        )


class Knowledge(FakeKnowledge):
    async def resolve(self, text: str, release: str) -> list[Entity]:
        return [
            Entity(iri="site", name="Bleida", kind="Site", status="candidate"),
            Entity(iri="project", name="P", kind="Project", status="approved"),
        ]

    async def documents(
        self, entities: list[str], release: str, cursor: str | None,
    ) -> DocumentPage:
        assert entities == ["site"]
        return await super().documents(entities, release, cursor)


def runner(search: Search | None = None, **kwargs: object) -> Arms:
    return Arms(
        search or Search(), Knowledge(), Planner(), None, "r1", "kg1", **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_parity_arms_match_golden_eval_semantic_modes() -> None:
    search = Search()
    a0 = await runner(search).run("A0", "Recovery?", Budget())
    assert len(a0.contexts) == 10 and a0.passage_mode == "truncate"
    assert search.calls[-1] == ("Recovery?", "semantic", [], 10)
    a1 = await runner(search).run("A1", "Recovery?", Budget())
    assert len(a1.contexts) == 10 and search.calls[-1][3] == 50
    # Focus demotes the filename-periodic summaries (even IDs) below primary reports.
    assert sum(c.source_role == "secondary_summary" for c in a1.contexts) < 5


@pytest.mark.asyncio
@pytest.mark.parametrize(("arm", "count", "limit"), [
    ("P16", 16, 1200), ("P12", 12, 1200), ("P24", 24, 1200), ("P16W2000", 16, 2000),
])
async def test_pack_arms_expose_policy_excerpts(arm: str, count: int, limit: int) -> None:
    output = await runner().run(arm, "Recovery?", Budget())
    assert output.passage_mode == "split" and len(output.contexts) == count
    assert all(len(c.text) <= limit and "87.5 %" in c.text for c in output.contexts)


@pytest.mark.asyncio
async def test_ceiling_exposes_all_fifty_full_chunks() -> None:
    output = await runner().run("CEIL", "Recovery?", Budget())
    assert len(output.contexts) == 50 and all(c.text == LONG for c in output.contexts)


@pytest.mark.asyncio
async def test_decomposition_unions_facets_and_merges_overlapping_spans() -> None:
    search, budget = Search(), Budget()
    output = await runner(search).run("DEC", "Compare recovery", budget)
    assert output.notes["facets"] == ["pilot recovery", "lab recovery"]
    assert {call[0].split("\n")[0] for call in search.calls} == {"pilot recovery", "lab recovery"}
    ids = [c.id.split("#")[0] for c in output.contexts]
    assert len(set(ids)) == 16  # same candidates for both facets; spans merged per evidence
    assert budget.model_calls == 1


@pytest.mark.asyncio
async def test_kg_boost_uses_precise_entities_and_exact_hashes() -> None:
    search = Search()
    output = await runner(search).run("KGB", "Bleida recovery", Budget())
    assert output.notes["entities"] == ["Site:candidate:Bleida"]
    assert output.notes["kg_documents"] == 1
    assert any(call[2] == [SOURCE.content_hash] for call in search.calls)
    assert any(call[2] == [] for call in search.calls)  # unrestricted leg kept


@pytest.mark.asyncio
async def test_agentic_retrieval_arm_and_per_arm_errors_are_recorded() -> None:
    async def knowledge_base(question: str) -> list[Context]:
        return [Context("k1", "h", "T", "s", [1], "Recovery 87.5 %")]

    questions = [GoldenQuestion(
        id="Q1", question="Recovery?", category="A", split="dev", key_facts=[],
    )]
    records: list[dict[str, object]] = []
    await evaluate(questions, ["AAR"], runner(knowledge_base=knowledge_base), 1, records.append)
    await evaluate(questions, ["AAR"], runner(), 1, records.append)
    assert records[0]["error"] is None and records[0]["exposed_characters"] == 15
    assert "needs --knowledge-base" in str(records[1]["error"])
    assert records[1]["contexts"] == []


@pytest.mark.asyncio
async def test_records_report_composition_and_usage() -> None:
    questions = [GoldenQuestion(
        id="Q2", question="Recovery?", category="B", split="dev", key_facts=[],
    )]
    records: list[dict[str, object]] = []
    await evaluate(questions, ["CEIL"], runner(), 1, records.append)
    [item] = records
    assert item["documents"] == 50 and item["dossier_cu_share"] == 0.5
    assert item["secondary_share"] == 0.5
    assert item["usage"]["retrieval_calls"] == 1  # type: ignore[index]


def test_cli_requires_live_confirmation_and_explicit_heldout() -> None:
    with pytest.raises(SystemExit):
        evaluation.main(["--golden", "g.jsonl"])
    with pytest.raises(SystemExit):
        evaluation.main(["--golden", "g.jsonl", "--split", "heldout", "--confirm-live"])
    assert set(ARMS) >= {"A0", "A1", "P16", "CAP", "LLM", "DEC", "KGB", "CEIL", "AAR"}


def test_direct_search_audit_separates_question_queries_from_reference_oracle() -> None:
    from crv_agent.evaluation.search_audit import probe_queries

    row = {"question": "Which lots contained organic matter?",
           "expected_answer": "Report CR/AR/FM/RP/42/18: two positive lots"}
    lexical, hybrid, oracle = probe_queries(row)
    assert lexical["text"] == hybrid["text"] == row["question"]
    assert not lexical["oracle"] and not hybrid["oracle"]
    assert oracle["oracle"] and oracle["text"] == '"CR/AR/FM/RP/42/18"'
    assert probe_queries({**row, "expected_answer": "Procedure CR/RC/P/01.00"})[-1][
        "text"
    ] == '"CR/RC/P/01.00"'
    assert probe_queries({**row, "expected_answer": "Reference without a report code"})[-1][
        "text"
    ] == "Reference without a report code"


@pytest.mark.asyncio
@pytest.mark.parametrize("hybrid", [False, True])
async def test_direct_search_audit_preserves_release_scope_full_passages_and_ranks(hybrid) -> None:
    from crv_agent.evaluation.search_audit import query

    content = "Full original source text.\n" * 100

    class Rows:
        async def get_count(self):
            return 2

        async def __aiter__(self):
            for projection_id in ("source-1", "source-2"):
                yield {"projection_id": projection_id, "content": content,
                       "release_id": "released", "original_page_numbers": [2, 3],
                       "@search.score": 1.5}

    class Client:
        async def search(self, **kwargs):
            assert kwargs["filter"] == "release_id eq 'released'"
            assert kwargs["search_text"] == "Original question"
            assert kwargs["top"] == 2
            assert (kwargs["query_type"] == "semantic") is hybrid
            if hybrid:
                assert kwargs["vector_queries"][0].text == "Original question"
            return Rows()

    result = await query(
        Client(), {"id": "Q398"},
        {"arm": "question_hybrid" if hybrid else "question_lexical",
         "text": "Original question", "oracle": False}, "released", 2,
    )
    assert result["error"] is None and result["total_count"] == 2
    assert [hit["rank"] for hit in result["hits"]] == [1, 2]
    assert result["hits"][0]["content"] == content
    assert result["hits"][0]["original_page_numbers"] == [2, 3]


@pytest.mark.asyncio
async def test_direct_search_audit_records_query_failures_without_dropping_the_case() -> None:
    from crv_agent.evaluation.search_audit import query

    class Unavailable:
        async def search(self, **kwargs):
            raise ConnectionError("private endpoint unavailable")

    result = await query(
        Unavailable(), {"id": "Q398"},
        {"arm": "question_lexical", "text": "Question", "oracle": False}, "released", 2,
    )
    assert result["id"] == "Q398"
    assert result["error"] == "ConnectionError"
    assert result["hits"] == [] and result["total_count"] is None
    assert result["latency_ms"] > 0


def test_direct_search_document_followup_requires_one_question() -> None:
    from crv_agent.evaluation.search_audit import main

    with pytest.raises(SystemExit):
        main([
            "--comparisons", "saved.jsonl", "--out-dir", "audit",
            "--endpoint", "https://search.test", "--index", "evidence",
            "--document-hash", "a" * 64, "--confirm-live",
        ])


def test_direct_search_document_followup_can_read_a_large_scoped_document(monkeypatch) -> None:
    from crv_agent.evaluation import search_audit

    async def fake_audit(args):
        assert args.top == 1000
        assert args.only == ["Q111"]
        assert args.document_hash == ["a" * 64]
        return {"questions": 1}

    monkeypatch.setattr(search_audit, "audit", fake_audit)
    assert search_audit.main([
        "--comparisons", "saved.jsonl", "--out-dir", "audit",
        "--endpoint", "https://search.test", "--index", "evidence",
        "--document-hash", "a" * 64, "--only", "Q111", "--top", "1000", "--confirm-live",
    ]) == 0
