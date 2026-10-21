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
            "source_uri": "Geometallurgy Cu/Dossier Cu/report.pdf" if i % 2 else "RA mai.pdf",
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
