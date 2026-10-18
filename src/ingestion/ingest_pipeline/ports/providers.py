"""Model and projection provider ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from ..domain.cu import AnalyzerProfile
from ..domain.projection import ProjectionReport
from ..domain.vectors import Vector


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One validated vector per input, in input order, or raise."""
        ...


class SearchProjector(Protocol):
    async def upload(
        self, documents: Sequence[Mapping[str, Any]], *, key_field: str
    ) -> ProjectionReport: ...

    async def delete(self, keys: Sequence[str], *, key_field: str) -> ProjectionReport:
        """Delete rows by key; a key that is already absent counts as accepted."""
        ...

    async def verify(
        self, expected: Mapping[str, str], *, key_field: str, digest_field: str
    ) -> dict[str, str]:
        """Return keys that are missing or carry a different digest than expected."""
        ...


@dataclass(frozen=True)
class LayoutRequest:
    """Options of one Document Intelligence analyze call; all of them shape the result."""

    model_id: str = "prebuilt-layout"
    pages: str | None = None
    """1-based original page list for DI's ``pages`` parameter, e.g. ``"3-5,7"``."""
    output_content_format: str = "markdown"
    string_index_type: str = "unicodeCodePoint"
    figures: bool = True
    features: tuple[str, ...] = ()
    locale: str | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "pages": self.pages,
            "output_content_format": self.output_content_format,
            "string_index_type": self.string_index_type,
            "figures": self.figures,
            "features": sorted(self.features),
            "locale": self.locale,
        }


@dataclass(frozen=True)
class LayoutSubmission:
    operation_url: str
    request_id: str | None = None
    retry_after: float | None = None


class LayoutStatus(StrEnum):
    NOT_STARTED = "notStarted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class LayoutPoll:
    status: LayoutStatus
    retry_after: float | None = None
    error: str | None = None
    result_bytes: int = 0
    """Size of the full response body written to the poll destination (succeeded only)."""


class LayoutProvider(Protocol):
    @property
    def api_version(self) -> str: ...

    @property
    def endpoint_host(self) -> str: ...

    async def submit(self, source: Path, request: LayoutRequest) -> LayoutSubmission:
        """Stream ``source`` for analysis. Raises ``SubmissionUnknownError`` when the service
        may have accepted the request but no Operation-Location was received."""
        ...

    async def poll(self, operation_url: str, destination: Path) -> LayoutPoll:
        """GET the operation; a succeeded body is streamed to ``destination``, never buffered.

        Raises ``RemoteResultMissingError`` when the operation no longer exists."""
        ...

    async def figure(self, operation_url: str, figure_id: str, destination: Path) -> int:
        """Stream one cropped figure image to ``destination``; return its size."""
        ...

    async def delete_result(self, operation_url: str) -> None: ...


@dataclass(frozen=True)
class AnalysisRequest:
    """Options of one Content Understanding ``analyzeBinary`` call; all shape the result."""

    analyzer_id: str
    content_range: str | None = None
    """1-based original pages, e.g. ``"3-5,7"``; ``None`` analyzes the whole input."""
    mime_type: str = "application/octet-stream"
    processing_location: str = "dataZone"
    string_encoding: str = "codePoint"

    def identity(self) -> dict[str, Any]:
        return {
            "analyzer_id": self.analyzer_id,
            "content_range": self.content_range,
            "mime_type": self.mime_type,
            "processing_location": self.processing_location,
            "string_encoding": self.string_encoding,
        }


class AnalysisStatus(StrEnum):
    NOT_STARTED = "NotStarted"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELED = "Canceled"


@dataclass(frozen=True)
class AnalysisPoll:
    status: AnalysisStatus
    retry_after: float | None = None
    error: str | None = None
    result_bytes: int = 0


class ContentAnalysisProvider(Protocol):
    @property
    def api_version(self) -> str: ...

    @property
    def endpoint_host(self) -> str: ...

    async def submit(self, source: Path, request: AnalysisRequest) -> LayoutSubmission:
        """Stream ``source`` raw. Raises ``SubmissionUnknownError`` when the service may
        have accepted the request but no Operation-Location was received."""
        ...

    async def poll(self, operation_url: str, destination: Path) -> AnalysisPoll:
        """GET the operation; a succeeded body is streamed to ``destination``."""
        ...

    async def file(self, operation_url: str, path: str, destination: Path) -> int:
        """Stream one result file (e.g. a figure crop) to ``destination``; return its size."""
        ...

    async def delete_result(self, operation_url: str) -> None: ...

    async def analyzer(self, analyzer_id: str) -> dict[str, Any]: ...

    async def defaults(self) -> dict[str, Any]: ...


class AnalyzerCatalog(Protocol):
    def profile(self, analyzer_id: str) -> AnalyzerProfile:
        """The verified profile, or ``ConfigurationError`` (never a silent substitute)."""
        ...
