"""Deterministic fakes for the evidence stages: DI Layout, embeddings and a Search index."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from ingest_pipeline.domain.cu import AnalyzerRegistry
from ingest_pipeline.domain.errors import (
    InvalidInputError,
    RemoteResultMissingError,
    TransientError,
)
from ingest_pipeline.domain.projection import ProjectionReport
from ingest_pipeline.domain.vectors import Vector
from ingest_pipeline.ports.providers import (
    AnalysisPoll,
    AnalysisRequest,
    AnalysisStatus,
    LayoutPoll,
    LayoutRequest,
    LayoutStatus,
    LayoutSubmission,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
OPERATION = (
    "https://fake.cognitiveservices.azure.com/documentintelligence/documentModels/"
    "prebuilt-layout/analyzeResults/{id}?api-version=2024-11-30"
)


def expand_pages(pages: str | None, total: int) -> list[int]:
    if not pages:
        return list(range(1, total + 1))
    numbers: list[int] = []
    for part in pages.split(","):
        first, _, last = part.partition("-")
        numbers.extend(range(int(first), int(last or first) + 1))
    return numbers


def di_document(
    pages: Sequence[Mapping[str, Any]],
    *,
    paragraphs: Sequence[Mapping[str, Any]] = (),
    tables: Sequence[Mapping[str, Any]] = (),
    figures: Sequence[Mapping[str, Any]] = (),
    sections: Sequence[Mapping[str, Any]] = (),
    index_type: str = "unicodeCodePoint",
) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "createdDateTime": "2026-01-01T00:00:00Z",
        "lastUpdatedDateTime": "2026-01-01T00:00:05Z",
        "analyzeResult": {
            "apiVersion": "2024-11-30",
            "modelId": "prebuilt-layout",
            "stringIndexType": index_type,
            "contentFormat": "markdown",
            "content": "",
            "pages": list(pages),
            "paragraphs": list(paragraphs),
            "tables": list(tables),
            "figures": list(figures),
            "sections": list(sections),
        },
    }


def di_page(number: int, *, text: bool = True, width: float = 8.27, height: float = 11.69) -> dict:
    page: dict[str, Any] = {
        "pageNumber": number,
        "angle": 0,
        "width": width,
        "height": height,
        "unit": "inch",
        "spans": [],
    }
    if text:
        page["words"] = [{"content": "x", "span": {"offset": 0, "length": 1}}]
        page["lines"] = [{"content": "x", "spans": []}]
    return page


def region(page: int, x: float = 1.0, y: float = 1.0) -> list[dict[str, Any]]:
    return [{"pageNumber": page, "polygon": [x, y, x + 2, y, x + 2, y + 0.5, x, y + 0.5]}]


def simple_layout(numbers: Sequence[int], *, figures: bool = True) -> dict[str, Any]:
    """One heading and one paragraph per page (and one figure), offsets in reading order."""

    paragraphs: list[dict[str, Any]] = []
    figure_items: list[dict[str, Any]] = []
    offset = 0
    for number in numbers:
        for role, text in (
            ("sectionHeading", f"Section {number}"),
            (None, f"Texte de la page {number} pour le broyeur et la pompe de circulation."),
        ):
            item: dict[str, Any] = {
                "content": text,
                "boundingRegions": region(number),
                "spans": [{"offset": offset, "length": len(text)}],
            }
            if role:
                item["role"] = role
            paragraphs.append(item)
            offset += len(text) + 2
        if figures:
            caption = f"Figure {number}: schema"
            paragraphs.append(
                {
                    "content": caption,
                    "boundingRegions": region(number, y=5),
                    "spans": [{"offset": offset, "length": len(caption)}],
                }
            )
            figure_items.append(
                {
                    "id": f"{number}.1",
                    "boundingRegions": region(number, y=4),
                    "spans": [{"offset": offset, "length": len(caption)}],
                    "caption": {
                        "content": caption,
                        "elements": [f"/paragraphs/{len(paragraphs) - 1}"],
                    },
                    "elements": [],
                }
            )
            offset += len(caption) + 2
    return di_document(
        [di_page(number) for number in numbers], paragraphs=paragraphs, figures=figure_items
    )


def pdf_page_count(data: bytes) -> int:
    document = pdfium.PdfDocument(data)
    try:
        return len(document)
    finally:
        document.close()


Responder = Callable[[LayoutRequest, bytes], dict[str, Any]]


def default_responder(request: LayoutRequest, data: bytes) -> dict[str, Any]:
    total = pdf_page_count(data) if data.startswith(b"%PDF") else 1
    return simple_layout(expand_pages(request.pages, total), figures=request.figures)


@dataclass
class FakeLayout:
    """``LayoutProvider``: scripted results, polls before completion and figure faults."""

    responder: Responder = default_responder
    polls_before_done: int = 1
    figure_faults: dict[str, list[Exception]] = field(default_factory=dict)
    fail_status: LayoutStatus | None = None
    submissions: list[tuple[LayoutRequest, int]] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    figure_calls: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    api_version: str = "2024-11-30"
    endpoint_host: str = "fake.cognitiveservices.azure.com"

    async def submit(self, source: Path, request: LayoutRequest) -> LayoutSubmission:
        data = await asyncio.to_thread(source.read_bytes)
        operation = OPERATION.format(id=f"op{len(self.submissions):06d}")
        self.submissions.append((request, len(data)))
        self.results[operation] = self.responder(request, data)
        return LayoutSubmission(operation_url=operation, request_id=f"req-{len(self.submissions)}")

    async def poll(self, operation_url: str, destination: Path) -> LayoutPoll:
        if operation_url not in self.results:
            raise RemoteResultMissingError("unknown operation")
        count = self.polls.get(operation_url, 0) + 1
        self.polls[operation_url] = count
        if count <= self.polls_before_done:
            return LayoutPoll(status=LayoutStatus.RUNNING, retry_after=2)
        if self.fail_status is not None:
            return LayoutPoll(status=self.fail_status, error="InvalidContent: corrupt page")
        body = json.dumps(self.results[operation_url]).encode()
        await asyncio.to_thread(destination.write_bytes, body)
        return LayoutPoll(status=LayoutStatus.SUCCEEDED, result_bytes=len(body))

    async def figure(self, operation_url: str, figure_id: str, destination: Path) -> int:
        self.figure_calls.append(figure_id)
        faults = self.figure_faults.get(figure_id)
        if faults:
            raise faults.pop(0)
        await asyncio.to_thread(destination.write_bytes, PNG + figure_id.encode())
        return len(PNG) + len(figure_id)

    async def delete_result(self, operation_url: str) -> None:
        self.deleted.append(operation_url)


def transient(message: str = "flaky") -> Exception:
    return TransientError(message)


CU_OPERATION = (
    "https://fake.services.ai.azure.com/contentunderstanding/analyzerResults/{id}"
    "?api-version=2025-11-01"
)


def cu_source(page: int, x: float = 1.0, y: float = 1.0) -> str:
    return f"D({page},{x},{y},{x + 2},{y},{x + 2},{y + 0.5},{x},{y + 0.5})"


def cu_content(
    numbers: Sequence[int],
    *,
    fields: Mapping[str, Any] | None = None,
    descriptions: bool = True,
) -> dict[str, Any]:
    """One heading, one paragraph and one described figure per page, offsets into Markdown."""

    markdown = ""
    paragraphs: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []

    def add(text: str) -> dict[str, int]:
        nonlocal markdown
        span = {"offset": len(markdown), "length": len(text)}
        markdown += text + "\n\n"
        return span

    for number in numbers:
        for role, text in (
            ("sectionHeading", f"Section {number}"),
            (None, f"Texte de la page {number} pour le broyeur 6140-ZM-2427 et la pompe."),
        ):
            item: dict[str, Any] = {
                "content": text, "source": cu_source(number), "span": add(text)
            }
            if role:
                item["role"] = role
            paragraphs.append(item)
        caption = f"Figure {number}: schema"
        paragraphs.append(
            {"content": caption, "source": cu_source(number, y=5), "span": add(caption)}
        )
        figure: dict[str, Any] = {
            "id": f"{number}.1",
            "source": cu_source(number, y=4),
            "span": dict(paragraphs[-1]["span"]),
            "caption": {"content": caption, "elements": [f"/paragraphs/{len(paragraphs) - 1}"]},
            "elements": [],
        }
        if descriptions:
            figure["description"] = f"Schema de procede de la page {number}."
        figures.append(figure)
    return {
        "path": "input1",
        "kind": "document",
        "markdown": markdown,
        "startPageNumber": min(numbers),
        "endPageNumber": max(numbers),
        "unit": "inch",
        "pages": [
            {
                "pageNumber": number,
                "width": 8.27,
                "height": 11.69,
                "spans": [],
                "words": [{"content": "x", "span": {"offset": 0, "length": 1}}],
            }
            for number in numbers
        ],
        "paragraphs": paragraphs,
        "figures": figures,
        "tables": [],
        "sections": [],
        "fields": dict(fields or {}),
    }


def cu_document(
    contents: Sequence[Mapping[str, Any]],
    *,
    analyzer_id: str = "tizert_doc_fields_v1",
    pages: int = 1,
    input_tokens: int = 1000,
    output_tokens: int = 100,
    warnings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "id": "op",
        "status": "Succeeded",
        "result": {
            "analyzerId": analyzer_id,
            "apiVersion": "2025-11-01",
            "createdAt": "2026-01-01T00:00:00Z",
            "stringEncoding": "codePoint",
            "warnings": list(warnings),
            "contents": list(contents),
        },
        "usage": {
            "documentPagesStandard": pages,
            "contextualizationTokens": 1000 * pages,
            "tokens": {
                "gpt-5.4-mini-input": input_tokens,
                "gpt-5.4-mini-output": output_tokens,
            },
        },
    }


AnalysisResponder = Callable[[AnalysisRequest, bytes], dict[str, Any]]

PFD_ANALYZER = "tizert_pfd_sheet_v1"


def fake_catalog() -> AnalyzerRegistry:
    """A verified catalog with one custom analyzer (extract + generate fields)."""

    return AnalyzerRegistry.load(
        {
            "apiVersion": "2025-11-01",
            "modelDeployments": {"gpt-5.4-mini": "dep-mini", "text-embedding-3-large": "dep-emb"},
            "modelVersions": {"dep-mini": "2026-03-17"},
            "analyzers": {
                PFD_ANALYZER: {
                    "baseAnalyzerId": "prebuilt-document",
                    "models": {"completion": "gpt-5.4-mini", "embedding": "text-embedding-3-large"},
                    "config": {"returnDetails": True, "enableFigureDescription": True},
                    "fieldSchema": {
                        "fields": {
                            "EquipmentTags": {"type": "array", "method": "extract"},
                            "Summary": {"type": "string", "method": "generate"},
                        }
                    },
                }
            },
        }
    )


def default_analysis(request: AnalysisRequest, data: bytes) -> dict[str, Any]:
    total = pdf_page_count(data) if data.startswith(b"%PDF") else 1
    numbers = expand_pages(request.content_range, total)
    content = cu_content(numbers)
    tag = "6140-ZM-2427"
    offset = content["markdown"].index(tag)
    content["fields"] = {
        "EquipmentTags": {
            "type": "array",
            "valueArray": [
                {
                    "type": "string",
                    "valueString": tag,
                    "spans": [{"offset": offset, "length": len(tag)}],
                    "source": cu_source(numbers[0]),
                    "confidence": 0.9,
                }
            ],
        },
        "Summary": {"type": "string", "valueString": "Schema du circuit de broyage."},
    }
    return cu_document([content], analyzer_id=request.analyzer_id, pages=len(numbers))


@dataclass
class FakeContentAnalysis:
    """``ContentAnalysisProvider``: scripted results, polls and analyzer definitions."""

    responder: AnalysisResponder = default_analysis
    polls_before_done: int = 1
    fail_status: AnalysisStatus | None = None
    transient_failures: int = 0
    """The first N finished analyses fail the way CU reports model throttling."""
    analyzers: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_settings: dict[str, Any] = field(default_factory=dict)
    submissions: list[tuple[AnalysisRequest, int]] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    file_calls: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    api_version: str = "2025-11-01"
    endpoint_host: str = "fake.services.ai.azure.com"

    async def submit(self, source: Path, request: AnalysisRequest) -> LayoutSubmission:
        data = await asyncio.to_thread(source.read_bytes)
        operation = CU_OPERATION.format(id=f"cu{len(self.submissions):06d}")
        self.submissions.append((request, len(data)))
        self.results[operation] = self.responder(request, data)
        return LayoutSubmission(operation_url=operation, request_id=f"cu-{len(self.submissions)}")

    async def poll(self, operation_url: str, destination: Path) -> AnalysisPoll:
        if operation_url not in self.results:
            raise RemoteResultMissingError("unknown operation")
        count = self.polls.get(operation_url, 0) + 1
        self.polls[operation_url] = count
        if count <= self.polls_before_done:
            return AnalysisPoll(status=AnalysisStatus.RUNNING, retry_after=2)
        if self.fail_status is not None:
            return AnalysisPoll(status=self.fail_status, error="InvalidContent: corrupt page")
        if self.transient_failures > 0:
            self.transient_failures -= 1
            return AnalysisPoll(
                status=AnalysisStatus.FAILED,
                error="InvalidRequest: ResourceError: Invalid request.",
            )
        body = json.dumps(self.results[operation_url]).encode()
        await asyncio.to_thread(destination.write_bytes, body)
        return AnalysisPoll(status=AnalysisStatus.SUCCEEDED, result_bytes=len(body))

    async def file(self, operation_url: str, path: str, destination: Path) -> int:
        self.file_calls.append(path)
        await asyncio.to_thread(destination.write_bytes, PNG + path.encode())
        return len(PNG) + len(path)

    async def delete_result(self, operation_url: str) -> None:
        self.deleted.append(operation_url)

    async def analyzer(self, analyzer_id: str) -> dict[str, Any]:
        if analyzer_id not in self.analyzers:
            raise InvalidInputError(f"CU analyzer read failed with HTTP 404: {analyzer_id}")
        return self.analyzers[analyzer_id]

    async def defaults(self) -> dict[str, Any]:
        return self.default_settings


def permanent(message: str = "bad figure") -> Exception:
    return InvalidInputError(message)


@dataclass
class FakeEmbeddings:
    model: str = "fake-embedding"
    dimensions: int = 8
    calls: list[int] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls.append(len(texts))
        vectors: list[Vector] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append(tuple(byte / 255.0 for byte in digest[: self.dimensions]))
        return vectors


@dataclass
class FakeIndex:
    """``SearchProjector`` over a dict; keys in ``invisible`` are not yet queryable."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    invisible: set[str] = field(default_factory=set)
    reject: set[str] = field(default_factory=set)
    reject_all: bool = False
    lagging_verifications: int = 0
    """This many verify calls report every key as missing (acknowledged, not yet visible)."""
    uploads: int = 0
    fields: frozenset[str] | None = None
    """Schema field names; like Search, a document with any other field is rejected."""

    async def upload(
        self, documents: Sequence[Mapping[str, Any]], *, key_field: str
    ) -> ProjectionReport:
        self.uploads += 1
        if any(str(document[key_field]).startswith("_") for document in documents):
            raise InvalidInputError("Search rejects document keys starting with '_'")
        accepted: list[str] = []
        permanent_keys: dict[str, str] = {}
        for document in documents:
            key = str(document[key_field])
            unknown = sorted(set(document) - self.fields) if self.fields is not None else []
            if unknown:
                permanent_keys[key] = f"HTTP 400: unknown field(s) {', '.join(unknown)}"
                continue
            if self.reject_all or key in self.reject:
                permanent_keys[key] = "HTTP 400: invalid field"
                continue
            self.rows[key] = dict(document)
            accepted.append(key)
        return ProjectionReport(tuple(accepted), {}, permanent_keys)

    async def delete(self, keys: Sequence[str], *, key_field: str) -> ProjectionReport:
        for key in keys:
            self.rows.pop(key, None)
        return ProjectionReport(tuple(keys))

    async def verify(
        self, expected: Mapping[str, str], *, key_field: str, digest_field: str
    ) -> dict[str, str]:
        problems: dict[str, str] = {}
        if self.lagging_verifications > 0:
            self.lagging_verifications -= 1
            return dict.fromkeys(expected, "missing")
        for key, digest in expected.items():
            row = self.rows.get(key)
            if row is None or key in self.invisible:
                problems[key] = "missing"
            elif row.get(digest_field) != digest:
                problems[key] = "digest_mismatch"
        return problems
