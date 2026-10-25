from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from crv_agent.adapters.postgres.memory import serializer
from crv_agent.api.app import create_app
from crv_agent.api.dependencies import AppServices
from crv_agent.api.identity import LocalIdentity
from crv_agent.api.ranges import UnsatisfiableRange, byte_range
from crv_agent.orchestration.graph import build_graph
from crv_agent.ports.sources import SourceInfo, SourceNotFound
from tests.fakes import FakeKnowledge, FakeModels, FakeWorker, MemoryPersistence

DIGEST = "a" * 64
PDF = b"%PDF-1.7 " + bytes(range(256)) * 4


class FakeSources:
    async def stat(self, digest: str) -> SourceInfo:
        if digest != DIGEST:
            raise SourceNotFound(digest)
        return SourceInfo(size=len(PDF), etag="etag-1")

    async def read(self, digest: str, start: int, end: int) -> AsyncIterator[bytes]:
        yield PDF[start:end]


def client(sources: FakeSources | None = FakeSources()) -> TestClient:
    async def ready() -> None:
        return None

    services = AppServices(
        persistence=MemoryPersistence(), identity=LocalIdentity("owner"), models=FakeModels(),
        worker=FakeWorker(), knowledge=FakeKnowledge(),
        graph=build_graph(InMemorySaver(serde=serializer())),
        search_release="r1", kg_release="kg1", ready=ready,
    )
    return TestClient(create_app(replace(services, sources=sources)))


def test_source_pdf_is_served_whole_and_cacheable() -> None:
    response = client().get(f"/api/v1/sources/{DIGEST}.pdf")
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["etag"] == '"etag-1"'


def test_source_pdf_honours_byte_ranges() -> None:
    response = client().get(f"/api/v1/sources/{DIGEST}.pdf", headers={"Range": "bytes=2-9"})
    assert response.status_code == 206
    assert response.content == PDF[2:10]
    assert response.headers["content-range"] == f"bytes 2-9/{len(PDF)}"
    assert response.headers["content-length"] == "8"


def test_unsatisfiable_range_is_416() -> None:
    response = client().get(
        f"/api/v1/sources/{DIGEST}.pdf", headers={"Range": f"bytes={len(PDF)}-"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PDF)}"


@pytest.mark.parametrize("name", [
    "b" * 64 + ".pdf",  # unknown digest
    DIGEST,  # missing extension
    "../" + DIGEST + ".pdf",
    DIGEST[:-1] + "Z.pdf",
    DIGEST + ".pdf.exe",
])
def test_only_known_content_addressed_names_are_served(name: str) -> None:
    assert client().get(f"/api/v1/sources/{name}").status_code == 404


def test_sources_are_unavailable_when_not_configured() -> None:
    assert client(None).get(f"/api/v1/sources/{DIGEST}.pdf").status_code == 404


@pytest.mark.parametrize(("header", "size", "expected"), [
    (None, 100, None),
    ("bytes=0-", 100, (0, 100)),
    ("bytes=10-19", 100, (10, 20)),
    ("bytes=90-500", 100, (90, 100)),
    ("bytes=-10", 100, (90, 100)),
    ("bytes=-500", 100, (0, 100)),
    ("bytes=0-1,5-6", 100, None),
    ("items=0-1", 100, None),
    ("bytes=-", 100, None),
])
def test_byte_range(header: str | None, size: int, expected: tuple[int, int] | None) -> None:
    assert byte_range(header, size) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=50-10", "bytes=-0"])
def test_byte_range_rejects_unsatisfiable(header: str) -> None:
    with pytest.raises(UnsatisfiableRange):
        byte_range(header, 100)
