"""Content Understanding REST client against a local HTTP server (no Azure calls)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from tizertmaint_pipeline.adapters.azure_ai.content_understanding import (
    ContentUnderstandingClient,
)
from tizertmaint_pipeline.domain.errors import (
    ConfigurationError,
    IntegrityError,
    RemoteResultMissingError,
    ResourceExhaustedError,
    SubmissionUnknownError,
)
from tizertmaint_pipeline.ports.providers import AnalysisRequest, AnalysisStatus

RESULT_PATH = "/contentunderstanding/analyzerResults/3b31320d-8bab-4f88"
ANALYZE = "/contentunderstanding/analyzers/tizert_pfd_sheet_v1:analyzeBinary"

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
        return handler(request)

    def operation(self, host: str | None = None) -> str:
        return f"{host or self.base}{RESULT_PATH}?api-version=2025-11-01"


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
async def client(server: Server) -> AsyncIterator[ContentUnderstandingClient]:
    cu = ContentUnderstandingClient(
        endpoint=server.base, credential=None, max_result_bytes=4096, max_file_bytes=1024
    )
    try:
        yield cu
    finally:
        await cu.close()


def _accepted(location: str) -> Handler:
    return lambda _: web.Response(
        status=202, headers={"Operation-Location": location, "x-ms-request-id": "req-1"}
    )


async def test_submit_streams_the_raw_file_with_range_and_location(
    server: Server, client: ContentUnderstandingClient, tmp_path: Path
) -> None:
    source = tmp_path / "pfd.pdf"
    source.write_bytes(bytes(range(256)) * 40)
    server.routes[("POST", ANALYZE)] = _accepted(server.operation())
    request = AnalysisRequest(
        analyzer_id="tizert_pfd_sheet_v1", content_range="3-7", mime_type="application/pdf"
    )
    submission = await client.submit(source, request)

    sent = server.requests[0]
    assert sent["query"] == {
        "api-version": "2025-11-01",
        "processingLocation": "dataZone",
        "stringEncoding": "codePoint",
        "range": "3-7",
    }
    assert sent["headers"]["Content-Type"] == "application/pdf"
    assert int(sent["headers"]["Content-Length"]) == len(sent["body"])
    assert sent["body"] == source.read_bytes()
    assert submission.operation_url == server.operation()
    assert submission.request_id == "req-1"


async def test_submit_refuses_foreign_locations_and_bad_options(
    server: Server, client: ContentUnderstandingClient, tmp_path: Path
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.7")
    server.routes[("POST", ANALYZE)] = _accepted(server.operation("https://evil.example.com"))
    with pytest.raises(SubmissionUnknownError, match="untrusted"):
        await client.submit(source, AnalysisRequest(analyzer_id="tizert_pfd_sheet_v1"))
    server.routes[("POST", ANALYZE)] = lambda _: web.Response(status=202)
    with pytest.raises(SubmissionUnknownError):
        await client.submit(source, AnalysisRequest(analyzer_id="tizert_pfd_sheet_v1"))
    with pytest.raises(ConfigurationError):
        await client.submit(source, AnalysisRequest(analyzer_id="bad/../id"))
    with pytest.raises(ConfigurationError):
        await client.submit(
            source,
            AnalysisRequest(analyzer_id="tizert_pfd_sheet_v1", processing_location="anywhere"),
        )


async def test_poll_reports_running_success_and_failure(
    server: Server, client: ContentUnderstandingClient, tmp_path: Path
) -> None:
    destination = tmp_path / "result.json"
    bodies = iter(
        [
            {"id": "x", "status": "Running"},
            {"id": "x", "status": "Succeeded", "result": {"contents": []}},
            {"id": "x", "status": "Failed", "error": {"code": "InvalidContent", "message": "bad"}},
        ]
    )
    server.routes[("GET", RESULT_PATH)] = lambda _: web.json_response(
        next(bodies), headers={"Retry-After": "4"}
    )
    running = await client.poll(server.operation(), destination)
    assert (running.status, running.retry_after) == (AnalysisStatus.RUNNING, 4)
    assert not destination.exists()

    succeeded = await client.poll(server.operation(), destination)
    assert succeeded.status is AnalysisStatus.SUCCEEDED
    assert json.loads(destination.read_bytes())["result"] == {"contents": []}
    assert succeeded.result_bytes == destination.stat().st_size

    failed = await client.poll(server.operation(), destination)
    assert failed.status is AnalysisStatus.FAILED
    assert "bad" in (failed.error or "")


async def test_missing_results_and_oversized_bodies_are_classified(
    server: Server, client: ContentUnderstandingClient, tmp_path: Path
) -> None:
    with pytest.raises(RemoteResultMissingError):
        await client.poll(server.operation(), tmp_path / "missing.json")
    server.routes[("GET", RESULT_PATH)] = lambda _: web.json_response(
        {"status": "Succeeded", "pad": "x" * 8000}
    )
    with pytest.raises(ResourceExhaustedError):
        await client.poll(server.operation(), tmp_path / "big.json")


async def test_result_files_delete_and_analyzer_reads(
    server: Server, client: ContentUnderstandingClient, tmp_path: Path
) -> None:
    server.routes[("GET", f"{RESULT_PATH}/files/figure-1.1")] = lambda _: web.Response(
        body=b"\x89PNG-figure", content_type="image/png"
    )
    server.routes[("DELETE", RESULT_PATH)] = lambda _: web.Response(status=204)
    server.routes[("GET", "/contentunderstanding/analyzers/prebuilt-documentSearch")] = (
        lambda _: web.json_response({"analyzerId": "prebuilt-documentSearch", "models": {}})
    )
    server.routes[("GET", "/contentunderstanding/defaults")] = lambda _: web.json_response(
        {"modelDeployments": {"gpt-5.4-mini": "tizert-gpt-5.4-mini"}}
    )
    destination = tmp_path / "figure.png"
    size = await client.file(server.operation(), "figure-1.1", destination)
    assert destination.read_bytes() == b"\x89PNG-figure"
    assert size == len(b"\x89PNG-figure")
    with pytest.raises(IntegrityError):
        await client.file(server.operation(), "../secrets", destination)
    await client.delete_result(server.operation())
    assert server.requests[-1]["method"] == "DELETE"
    analyzer = await client.analyzer("prebuilt-documentSearch")
    assert analyzer["analyzerId"] == "prebuilt-documentSearch"
    defaults = await client.defaults()
    assert defaults["modelDeployments"] == {"gpt-5.4-mini": "tizert-gpt-5.4-mini"}
