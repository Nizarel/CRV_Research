import json
from unittest.mock import AsyncMock

import pytest
from azure.search.documents.aio import SearchClient

from crv_agent.adapters.search import AzureSearch
from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import Entity
from crv_agent.services.candidates import ranked_candidates
from crv_agent.services.evidence_pack import (
    EvidencePolicy,
    apply_ranking,
    build_pack,
    excerpt,
    locate,
)
from crv_agent.services.navigation import steering_entities
from crv_agent.services.retrieval import Retrieval
from crv_agent.tools.evidence import evidence_tools
from tests.fakes import SOURCE


def hits(n: int = 50, **changes: object) -> list[Evidence]:
    return [
        SOURCE.model_copy(update={
            "id": f"S{i}", "chunk_id": f"c{i}", "content_hash": f"{i:064x}", **changes,
        }) for i in range(n)
    ]


class Search:
    def __init__(self, results: list[Evidence]) -> None:
        self.results = results
        self.captions: list[bool] = []

    async def query(
        self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        captions: bool = False,
    ) -> list[Evidence]:
        self.captions.append(captions)
        return self.results[:top]

    async def ready(self) -> None:
        return None


class Reranker:
    def __init__(self, ranking: list[str]) -> None:
        self.ranking = ranking
        self.seen: list[tuple[str, str, str]] = []

    async def rerank(
        self, query: str, candidates: list[tuple[str, str, str]], budget: Budget,
    ) -> list[str]:
        self.seen = candidates
        await budget.reserve(model=1, tokens=100)
        return self.ranking


def test_policy_bounds() -> None:
    with pytest.raises(ValueError):
        EvidencePolicy(pack_size=0)
    with pytest.raises(ValueError):
        EvidencePolicy(excerpt_characters=9000, read_characters=8000)
    assert EvidencePolicy().pack_size == 16


@pytest.mark.parametrize(("size", "excerpt_chars"), [(12, 1200), (24, 1200), (16, 2000)])
def test_pack_sizes_follow_policy(size: int, excerpt_chars: int) -> None:
    content = "intro " * 800 + "recovery 87.5 % pilot " + "tail " * 800
    pack = build_pack(
        hits(content=content), "pilot recovery", [],
        EvidencePolicy(pack_size=size, excerpt_characters=excerpt_chars),
    )
    assert len(pack.shown) == size and len(pack.previews) == 50 - size
    for hit, (start, end) in pack.shown:
        assert end - start <= excerpt_chars
        assert "recovery 87.5 % pilot" in hit.content[start:end]


def test_caption_anchor_is_verbatim_with_whitespace_tolerance_and_lexical_fallback() -> None:
    content = (
        "alpha " * 600 + "La récupération\n du cuivre atteint 87,5 % au pilote."
        + " omega" * 600 + " zeta"
    )
    caption = "La récupération du cuivre atteint 87,5 % au pilote."
    assert locate(content, caption) is not None
    assert locate(content, "Texte qui n'existe pas dans le document") is None
    hit = SOURCE.model_copy(update={"content": content, "caption": caption})
    start, end = excerpt(hit, {"zeta": 1.0}, 600, "caption")
    assert "87,5 %" in content[start:end]
    lexical = excerpt(hit, {"zeta": 1.0}, 600, "lexical")
    assert "87,5 %" not in content[slice(*lexical)] and "zeta" in content[slice(*lexical)]
    fallback = hit.model_copy(update={"caption": "summarised wording not in the text"})
    assert excerpt(fallback, {"zeta": 1.0}, 600, "caption") == lexical


def test_apply_ranking_ignores_invented_and_duplicate_ids_and_keeps_every_hit() -> None:
    items = hits(5)
    result = apply_ranking(items, ["S3", "invented", "S3", "S1"])
    assert [hit.id for hit in result] == ["S3", "S1", "S0", "S2", "S4"]


@pytest.mark.asyncio
async def test_llm_rerank_reorders_candidates_within_budget() -> None:
    reranker, budget = Reranker(["S49", "S48"]), Budget()
    result = await ranked_candidates(
        Retrieval(Search(hits())), "Recovery?", "r1", [], budget,
        EvidencePolicy(rerank="llm"), reranker,
    )
    assert [hit.id for hit in result[:2]] == ["S49", "S48"] and len(result) == 50
    assert len(reranker.seen) == 50 and all(len(preview) <= 600 for *_, preview in reranker.seen)
    assert budget.model_calls == 1
    with pytest.raises(ValueError, match="needs a reranker"):
        await ranked_candidates(
            Retrieval(Search(hits())), "Recovery?", "r1", [], Budget(),
            EvidencePolicy(rerank="llm"), None,
        )


@pytest.mark.asyncio
async def test_caption_policy_requests_semantic_captions_only() -> None:
    search = Search(hits())
    tool, _ = evidence_tools(
        Retrieval(search), "r1", [], Budget(), {}, "Recovery 2019?",
        EvidencePolicy(anchor="caption", pack_size=4),
    )
    listing = json.loads(await tool.ainvoke({"query": "Recovery 2019"}))
    assert len(listing["evidence"]) == 4
    assert search.captions == [True, False]  # semantic leg, then lexical leg (digits)


@pytest.mark.asyncio
async def test_azure_adapter_maps_extractive_captions() -> None:
    class Caption:
        text = "Recovery 87.5 % at pilot scale."

    row = {
        "projection_id": "p1", "release_id": "r1", "chunk_id": "c1", "content_hash": "a" * 64,
        "title": "T", "content": "Recovery 87.5 % at pilot scale.", "source_uri": "s",
        "original_page_numbers": [1], "source_locators": [], "original_source_uris": [],
        "is_generated": False, "document_class": "report", "citation_basis": "source",
        "extraction_route": "layout", "original_content_hashes": [],
        "@search.captions": [Caption()],
    }

    async def rows():
        yield row

    client = AsyncMock(spec=SearchClient)
    client.search.return_value = rows()
    [evidence] = await AzureSearch(client, "r1").query("q", "r1", "semantic", [], captions=True)
    assert evidence.caption == Caption.text
    assert client.search.call_args.kwargs["query_caption"] == "extractive"
    client.search.return_value = rows()
    await AzureSearch(client, "r1").query("q", "r1", "lexical", [], captions=True)
    assert client.search.call_args.kwargs["query_caption"] is None


def test_steering_entities_are_precise_except_for_catalogue() -> None:
    entities = [
        Entity(iri=f"{kind}:{status}", name=kind, kind=kind, status=status)
        for kind, status in [
            ("Project", "approved"), ("Organisation", "candidate"),
            ("Organisation", "approved"), ("Site", "candidate"), ("ReportCode", "observed"),
            ("Site", "rejected"),
        ]
    ]
    assert [e.iri for e in steering_entities(entities, "lookup")] == [
        "Organisation:approved", "Site:candidate", "ReportCode:observed",
    ]
    assert steering_entities(entities, "catalogue") == entities
