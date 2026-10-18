"""Document Intelligence REST client against a local HTTP server (no Azure calls)."""

from __future__ import annotations

import base64
import json
import os
import tracemalloc
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from ingest_pipeline.adapters.azure_ai.document_intelligence import (
    DocumentIntelligenceLayout,
    base64_json_body,
    base64_json_length,
)
from ingest_pipeline.adapters.azure_ai.remote_operation import ApiKeyFile
from ingest_pipeline.domain.errors import (
    ConfigurationError,
    RemoteResultMissingError,
    ResourceExhaustedError,
    SubmissionUnknownError,
    ThrottledError,
)
from ingest_pipeline.ports.providers import LayoutRequest, LayoutStatus

RESULT_PATH = "/documentintelligence/documentModels/prebuilt-layout/analyzeResults/abcdef12"

Handler = Callable[[web.Request], Any]


class Server:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[dict[str, Any]] = []
        self.base = ""

    async def dispatch(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        handler = self.routes.get((request.method, request.path))
        if handler is None:
            return web.Response(status=404, text='{"error":{"code":"NotFound"}}')
        result = handler(request)
        return await result if hasattr(result, "__await__") else result

    def operation(self, host: str | None = None) -> str:
        return f"{host or self.base}{RESULT_PATH}?api-version=2024-11-30"


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    state = Server()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", state.dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    state.base = f"http://127.0.0.1:{port}"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.fixture
async def client(server: Server) -> AsyncIterator[DocumentIntelligenceLayout]:
    layout = DocumentIntelligenceLayout(
        endpoint=server.base, credential=None, max_result_bytes=4096, max_figure_bytes=1024
    )
    try:
        yield layout
    finally:
        await layout.close()


ANALYZE = "/documentintelligence/documentModels/prebuilt-layout:analyze"


async def test_submit_streams_base64_json_with_content_length(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(bytes(range(256)) * 50)
    server.routes[("POST", ANALYZE)] = lambda _: web.Response(
        status=202,
        headers={
            "Operation-Location": server.operation(),
            "apim-request-id": "req-1",
            "Retry-After": "3",
        },
    )
    request = LayoutRequest(pages="3-5,7", features=("ocrHighResolution",), locale="fr-FR")
    submission = await client.submit(source, request)

    sent = server.requests[0]
    assert sent["query"] == {
        "api-version": "2024-11-30",
        "outputContentFormat": "markdown",
        "stringIndexType": "unicodeCodePoint",
        "pages": "3-5,7",
        "features": "ocrHighResolution",
        "output": "figures",
        "locale": "fr-FR",
    }
    assert int(sent["headers"]["Content-Length"]) == len(sent["body"])
    assert base64.b64decode(json.loads(sent["body"])["base64Source"]) == source.read_bytes()
    assert submission.operation_url == server.operation()
    assert submission.request_id == "req-1"
    assert submission.retry_after == 3


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (202, {}, SubmissionUnknownError),
        (429, {"Retry-After": "7"}, ThrottledError),
    ],
)
async def test_submit_failures_are_classified(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path, status, headers, error
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.7")
    server.routes[("POST", ANALYZE)] = lambda _: web.Response(status=status, headers=headers)
    with pytest.raises(error):
        await client.submit(source, LayoutRequest())


async def test_foreign_operation_location_is_rejected(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.7")
    foreign = server.operation("https://attacker.example")
    server.routes[("POST", ANALYZE)] = lambda _: web.Response(
        status=202, headers={"Operation-Location": foreign}
    )
    with pytest.raises(ConfigurationError):
        await client.submit(source, LayoutRequest())
    with pytest.raises(ConfigurationError):
        client.validate_operation(server.operation().replace("2024-11-30", "2023-07-31"))


async def test_api_key_file_is_sent_as_the_subscription_key_and_reread_after_rotation(
    server: Server, tmp_path: Path
) -> None:
    key = tmp_path / "partner"
    key.write_text("first-key\n", encoding="utf-8")
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.7")
    server.routes[("POST", ANALYZE)] = lambda _: web.Response(
        status=202, headers={"Operation-Location": server.operation()}
    )
    layout = DocumentIntelligenceLayout(
        endpoint=server.base,
        credential=ApiKeyFile(key),
        max_result_bytes=4096,
        max_figure_bytes=1024,
    )
    try:
        await layout.submit(source, LayoutRequest())
        _rewrite(key, "second-key")
        await layout.submit(source, LayoutRequest())
    finally:
        await layout.close()

    sent = [request["headers"] for request in server.requests]
    assert [headers.get("Ocp-Apim-Subscription-Key") for headers in sent] == [
        "first-key",
        "second-key",
    ]
    assert not any("Authorization" in headers for headers in sent)
    _rewrite(key, " \n")
    with pytest.raises(ConfigurationError):
        _ = ApiKeyFile(key).key


def _rewrite(path: Path, text: str) -> None:
    before = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


async def test_poll_running_then_succeeded_streams_to_disk(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path
) -> None:
    states = iter(
        [
            web.Response(status=200, text='{"status":"running"}', headers={"Retry-After": "5"}),
            web.Response(
                status=200,
                text=json.dumps({"status": "succeeded", "analyzeResult": {"pages": []}}),
            ),
        ]
    )
    server.routes[("GET", RESULT_PATH)] = lambda _: next(states)
    destination = tmp_path / "result.json"

    running = await client.poll(server.operation(), destination)
    assert running.status is LayoutStatus.RUNNING
    assert running.retry_after == 5
    assert not destination.exists()

    done = await client.poll(server.operation(), destination)
    assert done.status is LayoutStatus.SUCCEEDED
    assert json.loads(destination.read_text())["status"] == "succeeded"
    assert done.result_bytes == destination.stat().st_size


async def test_poll_failures(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path
) -> None:
    destination = tmp_path / "result.json"
    server.routes[("GET", RESULT_PATH)] = lambda _: web.Response(
        status=200,
        text=json.dumps(
            {"status": "failed", "error": {"code": "InvalidRequest", "message": "Corrupt"}}
        ),
    )
    failed = await client.poll(server.operation(), destination)
    assert failed.status is LayoutStatus.FAILED
    assert "InvalidRequest" in (failed.error or "")

    server.routes[("GET", RESULT_PATH)] = lambda _: web.Response(
        status=200, text=json.dumps({"status": "succeeded", "pad": "x" * 5000})
    )
    with pytest.raises(ResourceExhaustedError):
        await client.poll(server.operation(), destination)
    assert not destination.exists()

    del server.routes[("GET", RESULT_PATH)]
    with pytest.raises(RemoteResultMissingError):
        await client.poll(server.operation(), destination)


async def test_figures_and_cleanup(
    server: Server, client: DocumentIntelligenceLayout, tmp_path: Path
) -> None:
    server.routes[("GET", f"{RESULT_PATH}/figures/2.1")] = lambda _: web.Response(
        body=b"\x89PNG-data"
    )
    server.routes[("DELETE", RESULT_PATH)] = lambda _: web.Response(status=204)
    destination = tmp_path / "figure.png"
    assert await client.figure(server.operation(), "2.1", destination) == len(b"\x89PNG-data")
    await client.delete_result(server.operation())
    assert server.requests[-1]["method"] == "DELETE"


async def test_base64_body_streams_in_bounded_memory(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    with source.open("wb") as handle:
        for _ in range(40):
            handle.write(b"\xab" * (1024 * 1024))
    tracemalloc.start()
    try:
        total = 0
        async for piece in base64_json_body(source):
            total += len(piece)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == base64_json_length(40 * 1024 * 1024)
    assert peak < 16 * 1024 * 1024
