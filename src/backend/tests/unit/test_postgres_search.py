import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from crv_agent.adapters.postgres.search import PostgresSearch, passage
from crv_agent.adapters.retrieval_models import AzureOpenAIQueryEmbedder, CohereReranker
from crv_agent.adapters.search import SELECT, SearchRow
from crv_agent.domain.execution import Budget
from crv_agent.evaluation.dataset import GoldenQuestion
from crv_agent.evaluation.retrieval import ARMS, Arms, evaluate

HASH = "a" * 64


def row(key: str, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "projection_id": key, "release_id": "r1", "chunk_id": f"c-{key}", "content_hash": HASH,
        "title": f"{key}.pdf - p. 2", "content": f"Recovery of {key}", "source_uri": f"raw/{key}",
        "source_path": f"Dossier Cu/{key}.pdf", "document_code": "RA-2019-05",
        "original_page_numbers": [2], "source_locators": [], "original_source_uris": [],
        "is_generated": False, "document_class": "report", "citation_basis": "source",
        "extraction_route": "di_layout", "original_content_hashes": [HASH], "score": 0.03,
    }
    return {**values, **changes}


class Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def connection(self) -> AsyncIterator["Pool"]:
        yield self

    async def execute(self, query: str, params: tuple[Any, ...]) -> Cursor:
        self.calls.append((query, params))
        return Cursor(self.rows)


class Embedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.5] * 3072


class Reverse:
    def __init__(self) -> None:
        self.documents: list[str] = []

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        self.documents = documents
        return list(range(len(documents)))[::-1][:-1]  # drops the first candidate


def search(rows: list[dict[str, Any]], reranker: Any = None) -> tuple[PostgresSearch, Pool]:
    pool = Pool(rows)
    return PostgresSearch(pool, "r1", Embedder(), reranker=reranker), pool  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_semantic_query_embeds_once_and_maps_rows_like_azure_search() -> None:
    adapter, pool = search([row("k1"), row("k2")])
    hits = await adapter.query("Bleida recovery", "r1", "semantic", [HASH], top=10)

    [(sql, params)] = pool.calls
    assert "crv_retrieval.search" in sql
    assert params[:4] == ("r1", "Bleida recovery", [HASH], "te3") and params[5] == 10
    assert len(params[4]) == 3072
    expected = SearchRow.model_validate({key: row("k1")[key] for key in SELECT}).evidence()
    assert hits[0] == expected and hits[0].caption is None
    assert adapter.embedder.texts == ["Bleida recovery"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_lexical_query_sends_no_vector_and_bad_hashes_never_reach_sql() -> None:
    adapter, pool = search([row("k1")])
    await adapter.query("RA-2019-05", "r1", "lexical", [], top=10)
    assert pool.calls[0][1][3:5] == (None, None)
    assert adapter.embedder.texts == []  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="Invalid content hash filter"):
        await adapter.query("x", "r1", "semantic", ["' OR 1=1"], top=10)
    assert len(pool.calls) == 1


@pytest.mark.asyncio
async def test_cross_encoder_reorders_fifty_candidates_then_cuts_to_top() -> None:
    reranker = Reverse()
    adapter, pool = search([row(f"k{i}") for i in range(4)], reranker)
    hits = await adapter.query("q", "r1", "semantic", [], top=3)

    assert pool.calls[0][1][5] == 50
    assert [hit.id for hit in hits] == ["k3", "k2", "k1"]  # unranked k0 is kept last
    assert reranker.documents[0].startswith("k0.pdf - p. 2\nRA-2019-05\nDossier Cu/k0.pdf\n")
    lexical = await adapter.query("q", "r1", "lexical", [], top=3)
    assert [hit.id for hit in lexical] == ["k0", "k1", "k2"]


@pytest.mark.asyncio
async def test_tables_validate_scope_and_send_sorted_pages() -> None:
    adapter, pool = search([row("t1")])
    assert await adapter.tables("r1", {}) == []
    [hit] = await adapter.tables("r1", {HASH: [4, 3, 3]})
    assert hit.id == "t1"
    assert json.loads(pool.calls[0][1][1]) == {HASH: [3, 4]}
    with pytest.raises(ValueError, match="Invalid neighboring table scope"):
        await adapter.tables("r1", {HASH: list(range(1, 17))})


@pytest.mark.asyncio
async def test_ready_needs_the_release_and_every_vector() -> None:
    adapter, _ = search([{"chunks": 4, "te3": 4, "m3": 0}])
    await adapter.ready()
    with pytest.raises(ValueError, match="no evidence"):
        await search([{"chunks": 0, "te3": 0, "m3": 0}])[0].ready()
    with pytest.raises(ValueError, match="1 chunk"):
        await search([{"chunks": 4, "te3": 3, "m3": 0}])[0].ready()


def test_passage_puts_title_and_keywords_before_content() -> None:
    hit = SearchRow.model_validate({key: row("k1")[key] for key in SELECT}).evidence()
    assert passage(hit).split("\n")[:3] == ["k1.pdf - p. 2", "RA-2019-05", "Dossier Cu/k1.pdf"]


@asynccontextmanager
async def server(handler: Any) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    test_server = TestServer(app)
    await test_server.start_server()
    try:
        yield str(test_server.make_url("/"))
    finally:
        await test_server.close()


async def token() -> str:
    return "token"


@pytest.mark.asyncio
async def test_embedder_retries_throttling_and_shares_one_call_per_text() -> None:
    import asyncio

    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        body = await request.json()
        assert request.path == "/openai/deployments/te3/embeddings"
        assert body == {"input": ["q"]}
        return web.json_response({"data": [{"index": 0, "embedding": [0.1] * 3072}]})

    async with server(handler) as url, ClientSession() as session:
        embedder = AzureOpenAIQueryEmbedder(session, url, "te3", token)
        first, second = await asyncio.gather(embedder.embed("q"), embedder.embed("q"))
        assert first == second and len(first) == 3072
        assert await embedder.embed("q") == first
    assert seen == ["Bearer token", "Bearer token"]


@pytest.mark.asyncio
async def test_embedder_rejects_wrong_dimensions_and_forgets_failures() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"index": 0, "embedding": [0.1] * 10}]})

    async with server(handler) as url, ClientSession() as session:
        embedder = AzureOpenAIQueryEmbedder(session, url, "te3", token)
        with pytest.raises(ValueError, match="unexpected shape"):
            await embedder.embed("q")
        assert embedder.cache == {}


@pytest.mark.asyncio
async def test_cohere_reranker_orders_by_relevance_and_checks_indexes() -> None:
    responses = [
        {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]},
        {"results": [{"index": 5, "relevance_score": 0.9}]},
    ]

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["model"] == "rerank" and body["top_n"] == 2
        return web.json_response(responses.pop(0))

    async with server(handler) as url, ClientSession() as session:
        reranker = CohereReranker(session, url + "v2/rerank", "rerank", token)
        assert await reranker.rerank("q", ["a", "b"]) == [1, 0]
        with pytest.raises(ValueError, match="unexpected shape"):
            await reranker.rerank("q", ["a", "b"])


def test_cohere_reranker_settings_need_an_https_url_and_model(monkeypatch) -> None:
    from crv_agent.runtime.settings import Settings

    for key in [key for key in os.environ if key.upper().startswith("CRV_")]:
        monkeypatch.delenv(key)
    base = {
        "search_endpoint": "https://search", "kg_release": "kg1", "postgres_host": "pg",
        "postgres_user": "u", "kg_database": "kg", "memory_user": "m",
        "openai_endpoint": "https://openai", "planner_deployment": "p",
        "worker_deployment": "w", "answer_deployment": "a", "verifier_deployment": "v",
        "local_owner": "dev",
    }
    assert Settings(**base).search_backend == "azure"
    with pytest.raises(ValueError, match="cohere reranker"):
        Settings(**base, search_backend="postgres", pg_reranker="cohere")
    settings = Settings(
        **base, search_backend="postgres", pg_reranker="cohere",
        pg_reranker_url="https://foundry/v2/rerank", pg_reranker_model="Cohere-rerank",
    )
    assert settings.pg_search_pool_size == 10


class LexicalSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def query(
        self, text: str, release: str, mode: str, hashes: list[str], top: int = 50,
        captions: bool = False,
    ) -> list[Any]:
        self.calls.append((mode, top))
        return []


@pytest.mark.asyncio
async def test_lexical_arm_and_backend_label() -> None:
    assert "LEX" in ARMS
    search_port = LexicalSearch()
    runner = Arms(search_port, None, None, None, "r1", "kg1")  # type: ignore[arg-type]
    output = await runner.run("LEX", "RA-2019-05", Budget())
    assert search_port.calls == [("lexical", 10)] and output.passage_mode == "truncate"

    records: list[dict[str, Any]] = []
    question = GoldenQuestion(id="Q1", question="q", category="A", split="dev", key_facts=[])
    await evaluate([question], ["LEX"], runner, 1, records.append, label="PG")
    assert records[0]["arm"] == "PG_LEX"
