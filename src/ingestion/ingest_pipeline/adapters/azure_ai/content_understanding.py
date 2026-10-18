"""Durable Content Understanding (GA 2025-11-01) client over raw REST.

``analyzeBinary`` streams the original file as the raw request body (no base64 inflation) with
the page ``range`` on the original, so returned page numbers stay original. Analyzers are only
read here, never created: authoring is an administrative release action.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from ...domain.cu import API_VERSION
from ...domain.errors import ConfigurationError, IntegrityError, SubmissionUnknownError
from ...ports.providers import AnalysisPoll, AnalysisRequest, AnalysisStatus, LayoutSubmission
from ..azure_errors import parse_retry_after
from .remote_operation import (
    SMALL_BODY_BYTES,
    RemoteOperationClient,
    discard,
    error_message,
    head,
    header,
    status_value,
)

_RESULT_PATH = re.compile(r"^/contentunderstanding/analyzerResults/[0-9A-Za-z-]{8,64}$")
_ANALYZER_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FILE_PATH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_STATUS = re.compile(rb'"status"\s*:\s*"([A-Za-z]+)"')
_PROCESSING_LOCATIONS = frozenset({"geography", "dataZone", "global"})
BODY_CHUNK = 4 * 1024 * 1024
MAX_DEFINITION_BYTES = 4 * 1024 * 1024


async def raw_body(path: Path, *, chunk_bytes: int = BODY_CHUNK) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while block := await asyncio.to_thread(handle.read, chunk_bytes):
            yield block


class ContentUnderstandingClient(RemoteOperationClient):
    """``ContentAnalysisProvider`` for custom and prebuilt document analyzers."""

    service = "Content Understanding"

    def __init__(
        self,
        *,
        endpoint: str,
        credential: Any | None,
        api_version: str = API_VERSION,
        max_result_bytes: int,
        max_file_bytes: int = 64 * 1024 * 1024,
        transport: Any | None = None,
        user_agent: str = "tizert-pipeline",
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            credential=credential,
            api_version=api_version,
            transport=transport,
            user_agent=user_agent,
        )
        self._max_result_bytes = max_result_bytes
        self._max_file_bytes = max_file_bytes

    async def submit(self, source: Path, request: AnalysisRequest) -> LayoutSubmission:
        if not _ANALYZER_ID.match(request.analyzer_id):
            raise ConfigurationError(f"Invalid analyzer id {request.analyzer_id!r}")
        if request.processing_location not in _PROCESSING_LOCATIONS:
            raise ConfigurationError(f"Invalid processing location {request.processing_location}")
        params = {
            "api-version": self._api_version,
            "processingLocation": request.processing_location,
            "stringEncoding": request.string_encoding,
        }
        if request.content_range:
            params["range"] = request.content_range
        analyzer = quote(request.analyzer_id, safe="")
        url = f"{self._base}/contentunderstanding/analyzers/{analyzer}:analyzeBinary"
        size = (await asyncio.to_thread(source.stat)).st_size
        headers = await self._accepted(
            f"{url}?{urlencode(params)}",
            headers={"Content-Type": request.mime_type, "Content-Length": str(size)},
            content=raw_body(source),
            operation="CU analyze",
        )
        location = header(headers, "operation-location")
        if not location:
            raise SubmissionUnknownError("CU accepted the request without an Operation-Location")
        try:
            operation_url = self.validate_operation(location)
        except ConfigurationError as error:
            # Accepted and billable: never let a retry submit it again unseen.
            raise SubmissionUnknownError(
                f"CU accepted the request but returned an untrusted Operation-Location: {error}"
            ) from error
        return LayoutSubmission(
            operation_url=operation_url,
            request_id=header(headers, "x-ms-request-id") or header(headers, "apim-request-id"),
            retry_after=parse_retry_after(headers),
        )

    def validate_operation(self, url: str) -> str:
        """Only ever send a bearer token to this resource's own analyzer-result URLs."""

        path, query = self._same_resource(url)
        if not _RESULT_PATH.match(path) or query.get("api-version") != self._api_version:
            raise ConfigurationError("Untrusted or unexpected CU Operation-Location")
        return f"{self._base}{path}?api-version={self._api_version}"

    async def poll(self, operation_url: str, destination: Path) -> AnalysisPoll:
        url = self.validate_operation(operation_url)
        _, headers, size = await self._download(
            "GET", url, destination, max_bytes=self._max_result_bytes, operation="CU poll"
        )
        retry_after = parse_retry_after(headers)
        status = await asyncio.to_thread(self._status, destination, size)
        if status in (AnalysisStatus.NOT_STARTED, AnalysisStatus.RUNNING):
            await asyncio.to_thread(discard, destination)
            return AnalysisPoll(status=status, retry_after=retry_after)
        if status is AnalysisStatus.SUCCEEDED:
            return AnalysisPoll(status=status, retry_after=retry_after, result_bytes=size)
        start = await asyncio.to_thread(head, destination, SMALL_BODY_BYTES)
        await asyncio.to_thread(discard, destination)
        return AnalysisPoll(status=status, error=error_message(start, status.value)[:2000])

    async def file(self, operation_url: str, path: str, destination: Path) -> int:
        if not _FILE_PATH.match(path):
            raise IntegrityError(f"Unexpected CU result file path {path!r}")
        base = self.validate_operation(operation_url).split("?", 1)[0]
        url = f"{base}/files/{quote(path, safe='/')}?api-version={self._api_version}"
        _, _, size = await self._download(
            "GET", url, destination, max_bytes=self._max_file_bytes, operation="CU result file"
        )
        return size

    async def delete_result(self, operation_url: str) -> None:
        await self._delete(self.validate_operation(operation_url), operation="CU delete")

    async def analyzer(self, analyzer_id: str) -> dict[str, Any]:
        if not _ANALYZER_ID.match(analyzer_id):
            raise ConfigurationError(f"Invalid analyzer id {analyzer_id!r}")
        url = (
            f"{self._base}/contentunderstanding/analyzers/{quote(analyzer_id, safe='')}"
            f"?api-version={self._api_version}"
        )
        return await self._get_json(
            url, operation="CU analyzer read", max_bytes=MAX_DEFINITION_BYTES
        )

    async def defaults(self) -> dict[str, Any]:
        url = f"{self._base}/contentunderstanding/defaults?api-version={self._api_version}"
        return await self._get_json(
            url, operation="CU defaults read", max_bytes=MAX_DEFINITION_BYTES
        )

    @staticmethod
    def _status(path: Path, size: int) -> AnalysisStatus:
        value = status_value(path, size, _STATUS)
        try:
            return AnalysisStatus(str(value))
        except ValueError as error:
            raise IntegrityError(f"CU returned an unknown operation status {value!r}") from error
