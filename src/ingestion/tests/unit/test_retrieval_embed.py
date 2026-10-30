"""retrieval-embed: the OpenAI-compatible provider and the resumable embedding_m3 backfill."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer

from ingest_pipeline.adapters.local_ai.embeddings import (
    OpenAICompatibleEmbeddings,
    check_endpoint,
    read_key_file,
)
from ingest_pipeline.adapters.memory.clock import ManualClock
from ingest_pipeline.adapters.memory.evidence import InMemoryEvidenceStore
from ingest_pipeline.cli import build_parser
from ingest_pipeline.domain.errors import (
    AuthError,
    ConfigurationError,
    IntegrityError,
    InvalidInputError,
    ThrottledError,
)
from ingest_pipeline.domain.ids import sha256_hex
from ingest_pipeline.domain.vectors import Vector
from ingest_pipeline.runtime.retrieval_embed import EmbedOptions, embed_release

CLOCK = ManualClock(datetime(2026, 10, 28, tzinfo=UTC))


@asynccontextmanager
async def server(handler: Any) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/v1/embeddings", handler)
    test_server = TestServer(app)
    await test_server.start_server()
    try:
        yield str(test_server.make_url("/v1"))
    finally:
        await test_server.close()


def vector(seed: float, dimensions: int = 4) -> list[float]:
    return [seed + index for index in range(1, dimensions + 1)]


def provider(session: ClientSession, url: str, **changes: Any) -> OpenAICompatibleEmbeddings:
    options: dict[str, Any] = {
        "base_url": url, "model": "BAAI/bge-m3", "served_model": "bge-m3", "dimensions": 4,
        "max_inputs_per_request": 2, "attempts": 3, **changes,
    }
    return OpenAICompatibleEmbeddings(session, **options)


@pytest.mark.parametrize(("url", "hosts"), [
    ("https://models.example/v1", ()), ("http://127.0.0.1:8081/v1/", ()),
    ("http://dgx-niz01:8081/v1", ("DGX-NIZ01",)),
])
def test_endpoint_accepts_https_and_private_http(url: str, hosts: tuple[str, ...]) -> None:
    assert check_endpoint(url, hosts) == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://dgx-niz01:8081/v1", "https://models.example/v2", "https://u:p@models.example/v1",
    "https://models.example/v1?x=1", "ftp://127.0.0.1/v1",
])
def test_endpoint_rejects_unsafe_or_wrong_urls(url: str) -> None:
    with pytest.raises(ConfigurationError):
        check_endpoint(url, ())


def test_key_file_is_read_and_blank_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("secret\n", encoding="utf-8")
    assert read_key_file(key) == "secret"
    key.write_text(" \n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="empty"):
        read_key_file(key)


async def test_provider_batches_orders_and_sends_the_served_name_and_key() -> None:
    seen: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append({"auth": request.headers.get("Authorization"), **body})
        data = [{"index": i, "embedding": vector(float(len(text)))}
                for i, text in enumerate(body["input"])]
        return web.json_response({"data": list(reversed(data))})

    async with server(handler) as url, ClientSession() as session:
        vectors = await provider(session, url, api_key="k").embed(["a", "bb", "ccc"])
    assert [v[0] for v in vectors] == [2.0, 3.0, 4.0]
    assert sorted(len(call["input"]) for call in seen) == [1, 2]
    assert {call["model"] for call in seen} == {"bge-m3"}
    assert {call["auth"] for call in seen} == {"Bearer k"}


async def test_provider_retries_throttling_then_succeeds() -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        return web.json_response({"data": [{"index": 0, "embedding": vector(1)}]})

    async with server(handler) as url, ClientSession() as session:
        assert len(await provider(session, url).embed(["a"])) == 1
    assert calls == 2


@pytest.mark.parametrize(("status", "body", "error"), [
    (400, None, InvalidInputError),
    (401, None, AuthError),
    (429, None, ThrottledError),
    (200, {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}, IntegrityError),
    (200, {"data": [{"index": 0, "embedding": [0.0] * 4}]}, IntegrityError),
    (200, {"object": "list"}, IntegrityError),
])
async def test_provider_classifies_failures(
    status: int, body: dict[str, Any] | None, error: type[Exception]
) -> None:
    async def handler(request: web.Request) -> web.Response:
        if status != 200:
            return web.Response(status=status, text="nope", headers={"Retry-After": "0"})
        return web.json_response(body)

    async with server(handler) as url, ClientSession(timeout=ClientTimeout(total=5)) as session:
        with pytest.raises(error):
            await provider(session, url).embed(["a"])


@dataclass(frozen=True)
class Row:
    projection_id: str
    content: str


class FakeStore:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = {row.projection_id: row for row in rows}
        self.vectors: dict[str, list[float]] = {}

    async def missing(self, release_id: str, *, after: str, limit: int) -> list[Row]:
        keys = sorted(k for k in self.rows if k > after and k not in self.vectors)
        return [self.rows[key] for key in keys[:limit]]

    async def write(self, rows: Sequence[Mapping[str, Any]]) -> int:
        written = 0
        for row in rows:
            current = self.rows[row["projection_id"]]
            if row["projection_id"] not in self.vectors and (
                sha256_hex(current.content) == row["content_sha256"]
            ):
                self.vectors[row["projection_id"]] = row["vector"]
                written += 1
        return written

    async def status(self, release_id: str) -> dict[str, int]:
        return {"chunks": len(self.rows), "te3": len(self.rows), "m3": len(self.vectors)}


class FakeProvider:
    model, dimensions = "BAAI/bge-m3", 4

    def __init__(self, reject: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.reject = reject or set()

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls.append(list(texts))
        if self.reject & set(texts):
            raise InvalidInputError("too long")
        return [tuple(vector(float(len(text)))) for text in texts]


async def test_backfill_uses_the_cache_dedupes_and_resumes() -> None:
    rows = [Row("p1", "alpha"), Row("p2", "beta"), Row("p3", "alpha"), Row("p4", "gamma")]
    store, cache, model = FakeStore(rows), InMemoryEvidenceStore(), FakeProvider()
    await cache.put_many({sha256_hex("gamma"): tuple(vector(9.0))}, model=model.model,
                         dimensions=4, now=CLOCK.now())
    first = await embed_release(store, model, cache, CLOCK,
                                EmbedOptions("crv-v1", batch_rows=2, limit=2))
    assert (first.selected, first.written, first.after["m3"]) == (2, 2, 2)
    second = await embed_release(store, model, cache, CLOCK, EmbedOptions("crv-v1", batch_rows=2))
    assert (second.selected, second.cached, second.embedded, second.written) == (2, 2, 0, 2)
    assert second.after == {"chunks": 4, "te3": 4, "m3": 4}
    assert store.vectors["p4"] == vector(9.0)
    assert model.calls == [["alpha", "beta"]]
    again = await embed_release(store, model, cache, CLOCK, EmbedOptions("crv-v1"))
    assert (again.selected, again.written, again.failed) == (0, 0, 0)


async def test_rejected_text_is_reported_and_skipped_not_blocking() -> None:
    rows = [Row("p1", "ok"), Row("p2", "x" * 50), Row("p3", "fine")]
    store, model = FakeStore(rows), FakeProvider(reject={"x" * 50})
    report = await embed_release(store, model, InMemoryEvidenceStore(), CLOCK,
                                 EmbedOptions("crv-v1"))
    assert (report.written, report.failed, report.failed_preview) == (2, 1, ["p2"])
    assert set(store.vectors) == {"p1", "p3"}


async def test_dry_run_reads_counts_only() -> None:
    store, model = FakeStore([Row("p1", "a")]), FakeProvider()
    report = await embed_release(store, model, InMemoryEvidenceStore(), CLOCK,
                                 EmbedOptions("crv-v1", dry_run=True))
    assert report.before == report.after == {"chunks": 1, "te3": 1, "m3": 0}
    assert model.calls == []
    assert store.vectors == {}


def test_options_and_parser() -> None:
    with pytest.raises(InvalidInputError):
        EmbedOptions("r", batch_rows=0)
    with pytest.raises(InvalidInputError):
        EmbedOptions("r", limit=0)
    args = build_parser().parse_args(
        ["retrieval-embed", "--release", "crv-v1", "--limit", "10", "--dry-run"]
    )
    assert (args.release, args.limit, args.dry_run, args.batch_rows) == ("crv-v1", 10, True, 256)
