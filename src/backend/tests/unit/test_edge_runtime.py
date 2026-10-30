"""Edge (Azure-independent) runtime configuration: local endpoints, M3 search, no credential."""

import os
from contextlib import AsyncExitStack
from typing import Any

import pytest
from aiohttp import ClientSession, web

from crv_agent.adapters.retrieval_models import (
    CohereReranker,
    OpenAICompatibleQueryEmbedder,
    TeiReranker,
)
from crv_agent.runtime.settings import endpoint_allowed
from tests.unit.test_postgres_search import server

BASE = {
    "kg_release": "kg1", "postgres_host": "postgres", "postgres_user": "crv_edge_agent",
    "kg_database": "ingestion-catalog", "memory_user": "crv_edge_agent",
    "planner_deployment": "p", "worker_deployment": "w", "answer_deployment": "a",
    "verifier_deployment": "v", "local_owner": "edge",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.upper().startswith("CRV_")]:
        monkeypatch.delenv(key)


def edge(tmp_path: Any, **overrides: Any) -> Any:
    from crv_agent.runtime.settings import Settings

    secret = tmp_path / "pg.password"
    secret.write_text("pw", encoding="utf-8")
    values = {
        **BASE, "postgres_auth": "password", "postgres_sslmode": "disable",
        "postgres_password_file": str(secret), "search_backend": "postgres",
        "pg_vector_model": "m3", "pg_embedder": "openai",
        "pg_embedding_url": "http://dgx-niz01:8081/v1", "pg_embedding_model": "bge-m3",
        "pg_reranker": "tei", "pg_reranker_url": "http://dgx-niz01:8082/rerank",
        "model_endpoint": "http://dgx-niz02:8000/v1", "model_auth": "none",
        "private_http_hosts": ["dgx-niz01", "dgx-niz02"],
    }
    return Settings(**{**values, **overrides})


def test_all_local_settings_need_no_azure_credential(tmp_path: Any) -> None:
    from crv_agent.runtime.infrastructure import needs_azure_credential

    settings = edge(tmp_path)
    assert settings.search_endpoint is None and settings.openai_endpoint is None
    assert needs_azure_credential(settings) is False
    assert needs_azure_credential(edge(tmp_path, pg_reranker="cohere",
                                       pg_reranker_url="https://foundry/v2/rerank",
                                       pg_reranker_model="rerank")) is True
    assert needs_azure_credential(edge(tmp_path, memory_auth="entra", memory_sslmode="verify-full"))
    assert not needs_azure_credential(
        edge(tmp_path, memory_auth="entra", memory_sslmode="verify-full"), memory=False,
    )


def test_azure_configuration_still_requires_its_endpoints() -> None:
    from crv_agent.runtime.infrastructure import needs_azure_credential
    from crv_agent.runtime.settings import Settings

    azure = {**BASE, "postgres_user": "u", "memory_user": "m"}
    with pytest.raises(ValueError, match="search endpoint"):
        Settings(**azure, openai_endpoint="https://openai")
    with pytest.raises(ValueError, match="model endpoint or an Azure OpenAI endpoint"):
        Settings(**azure, search_endpoint="https://search")
    settings = Settings(**azure, search_endpoint="https://search", openai_endpoint="https://o")
    assert settings.effective_model_auth == "entra" and needs_azure_credential(settings)
    with pytest.raises(ValueError, match="Azure OpenAI endpoint"):
        Settings(**azure, search_endpoint="https://s", search_backend="postgres",
                 model_endpoint="https://foundry/openai/v1", model_api_key="k")


@pytest.mark.parametrize(("url", "hosts", "allowed"), [
    ("https://foundry.example/openai/v1", [], True),
    ("http://127.0.0.1:8000/v1", [], True),
    ("http://localhost:8000/v1", [], True),
    ("http://dgx-niz02:8000/v1", [], False),
    ("http://dgx-niz02:8000/v1", ["DGX-NIZ02"], True),
    ("http://user:pw@127.0.0.1/v1", [], False),
    ("http://127.0.0.1/v1?x=1", [], False),
    ("http://127.0.0.1/v2", [], False),
    ("ftp://127.0.0.1/v1", [], False),
])
def test_endpoint_rule(url: str, hosts: list[str], allowed: bool) -> None:
    assert endpoint_allowed(url, hosts, v1=True) is allowed


def test_invalid_edge_combinations_are_rejected(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="te3 vector column"):
        edge(tmp_path, pg_embedder="azure", openai_endpoint="https://o")
    with pytest.raises(ValueError, match="openai embedder"):
        edge(tmp_path, pg_embedding_url="http://unlisted:8081/v1")
    with pytest.raises(ValueError, match="openai embedder"):
        edge(tmp_path, pg_embedding_model="")
    with pytest.raises(ValueError, match="local reranker"):
        edge(tmp_path, pg_reranker_url="http://unlisted/rerank")
    with pytest.raises(ValueError, match="vllm reranker"):
        edge(tmp_path, pg_reranker="vllm", pg_reranker_url="http://dgx-niz01:8082/v1/rerank")
    with pytest.raises(ValueError, match="HTTPS origin"):
        edge(tmp_path, model_auth="entra")
    with pytest.raises(ValueError, match="must not configure"):
        edge(tmp_path, model_api_key="k")
    with pytest.raises(ValueError, match="not both"):
        edge(tmp_path, sources_root="/srv", sources_account_url="https://blob")


@pytest.mark.asyncio
async def test_chat_models_use_local_endpoint_without_credential(tmp_path: Any) -> None:
    from crv_agent.runtime.infrastructure import UNAUTHENTICATED_KEY, create_chat_models

    async with AsyncExitStack() as stack:
        models = create_chat_models(stack, edge(tmp_path), None)
        assert {m.openai_api_base for m in models.values()} == {"http://dgx-niz02:8000/v1/"}
        assert models["planner"].openai_api_key.get_secret_value() == UNAUTHENTICATED_KEY
        key = tmp_path / "model.key"
        key.write_text("local-key\n", encoding="utf-8")
        keyed = create_chat_models(
            stack, edge(tmp_path, model_auth=None, model_api_key_file=str(key)), None,
        )
        assert keyed["worker"].openai_api_key.get_secret_value() == "local-key"
        with pytest.raises(ValueError, match="Azure credential"):
            create_chat_models(stack, edge(tmp_path, model_auth="entra",
                                           model_endpoint="https://f/openai/v1"), None)


@pytest.mark.asyncio
async def test_openai_compatible_embedder_sends_model_and_no_auth_header() -> None:
    seen: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append({"path": request.path, "auth": request.headers.get("Authorization"),
                     "body": await request.json()})
        return web.json_response({"data": [{"index": 0, "embedding": [0.5] * 1024}]})

    async with server(handler) as url, ClientSession() as session:
        embedder = OpenAICompatibleQueryEmbedder(session, url + "v1/", "bge-m3", None,
                                                 dimensions=1024)
        assert len(await embedder.embed("q")) == 1024
        wrong = OpenAICompatibleQueryEmbedder(session, url + "v1", "bge-m3", None, dimensions=3072)
        with pytest.raises(ValueError, match="unexpected shape"):
            await wrong.embed("q")
    assert seen[0] == {"path": "/v1/embeddings", "auth": None,
                       "body": {"model": "bge-m3", "input": ["q"]}}


@pytest.mark.asyncio
async def test_local_rerankers_order_candidates() -> None:
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        assert request.headers.get("Authorization") is None
        if request.path == "/rerank":
            assert body == {"query": "q", "texts": ["a", "b", "c"], "truncate": True}
            return web.json_response([
                {"index": 2, "score": 0.9}, {"index": 0, "score": 0.5}, {"index": 1, "score": 0.1},
            ])
        assert body["model"] == "bge-reranker-v2-m3" and body["top_n"] == 3
        return web.json_response({"results": [
            {"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.3},
        ]})

    async with server(handler) as url, ClientSession() as session:
        assert await TeiReranker(session, url + "rerank", None).rerank(
            "q", ["a", "b", "c"],
        ) == [2, 0, 1]
        assert await CohereReranker(
            session, url + "v1/rerank", "bge-reranker-v2-m3", None,
        ).rerank("q", ["a", "b", "c"]) == [1, 0]


@pytest.mark.asyncio
async def test_tei_reranker_rejects_bad_indexes() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response([{"index": 5, "score": 1.0}])

    async with server(handler) as url, ClientSession() as session:
        with pytest.raises(ValueError, match="unexpected shape"):
            await TeiReranker(session, url + "rerank", None).rerank("q", ["a"])
