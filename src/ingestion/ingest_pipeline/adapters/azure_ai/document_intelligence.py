"""Durable Document Intelligence v4.0 (2024-11-30) Layout client over raw REST.

The request body is streamed as base64 JSON; see ``remote_operation`` for why REST is used.
"""

from __future__ import annotations

import asyncio
import base64
import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from ...domain.errors import ConfigurationError, IntegrityError, SubmissionUnknownError
from ...ports.providers import LayoutPoll, LayoutRequest, LayoutStatus, LayoutSubmission
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

API_VERSION = "2024-11-30"
_RESULT_PATH = re.compile(
    r"^/documentintelligence/documentModels/[A-Za-z0-9][A-Za-z0-9._~-]{1,63}"
    r"/analyzeResults/[0-9A-Za-z-]{8,64}$"
)
_FIGURE_ID = re.compile(r"^\d{1,5}\.\d{1,5}$")
_STATUS = re.compile(rb'"status"\s*:\s*"([A-Za-z]+)"')
BODY_CHUNK = 3 * 1024 * 1024
"""Multiple of 3 so base64 pieces concatenate to the base64 of the whole file."""


async def base64_json_body(
    path: Path, *, chunk_bytes: int = BODY_CHUNK
) -> AsyncIterator[bytes]:
    """``{"base64Source":"..."}`` produced chunk by chunk; memory stays O(chunk)."""

    if chunk_bytes % 3:
        raise ValueError("chunk_bytes must be a multiple of 3")
    yield b'{"base64Source":"'
    with path.open("rb") as handle:
        while block := await asyncio.to_thread(handle.read, chunk_bytes):
            yield base64.b64encode(block)
    yield b'"}'


def base64_json_length(size: int) -> int:
    return len(b'{"base64Source":"') + 4 * math.ceil(size / 3) + len(b'"}')


class DocumentIntelligenceLayout(RemoteOperationClient):
    """``LayoutProvider`` for ``prebuilt-layout``."""

    service = "Document Intelligence"

    def __init__(
        self,
        *,
        endpoint: str,
        credential: Any | None,
        api_version: str = API_VERSION,
        max_result_bytes: int,
        max_figure_bytes: int = 64 * 1024 * 1024,
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
        self._max_figure_bytes = max_figure_bytes

    async def submit(self, source: Path, request: LayoutRequest) -> LayoutSubmission:
        params: dict[str, str] = {
            "api-version": self._api_version,
            "outputContentFormat": request.output_content_format,
            "stringIndexType": request.string_index_type,
        }
        if request.pages:
            params["pages"] = request.pages
        if request.features:
            params["features"] = ",".join(sorted(request.features))
        if request.figures:
            params["output"] = "figures"
        if request.locale:
            params["locale"] = request.locale
        model = quote(request.model_id, safe="")
        path = f"/documentintelligence/documentModels/{model}:analyze"
        size = (await asyncio.to_thread(source.stat)).st_size
        headers = await self._accepted(
            f"{self._base}{path}?{urlencode(params)}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(base64_json_length(size)),
            },
            content=base64_json_body(source),
            operation="DI analyze",
        )
        location = header(headers, "operation-location")
        if not location:
            raise SubmissionUnknownError("DI accepted the request without an Operation-Location")
        return LayoutSubmission(
            operation_url=self.validate_operation(location),
            request_id=header(headers, "apim-request-id"),
            retry_after=parse_retry_after(headers),
        )

    def validate_operation(self, url: str) -> str:
        """Only ever send a bearer token to this resource's own analyze-result URLs."""

        path, query = self._same_resource(url)
        if not _RESULT_PATH.match(path) or query.get("api-version") != self._api_version:
            raise ConfigurationError("Untrusted or unexpected DI Operation-Location")
        return f"{self._base}{path}?api-version={self._api_version}"

    async def poll(self, operation_url: str, destination: Path) -> LayoutPoll:
        url = self.validate_operation(operation_url)
        _, headers, size = await self._download(
            "GET", url, destination, max_bytes=self._max_result_bytes, operation="DI poll"
        )
        retry_after = parse_retry_after(headers)
        status = await asyncio.to_thread(self._status, destination, size)
        if status in (LayoutStatus.NOT_STARTED, LayoutStatus.RUNNING):
            await asyncio.to_thread(discard, destination)
            return LayoutPoll(status=status, retry_after=retry_after)
        if status is LayoutStatus.SUCCEEDED:
            return LayoutPoll(status=status, retry_after=retry_after, result_bytes=size)
        start = await asyncio.to_thread(head, destination, SMALL_BODY_BYTES)
        await asyncio.to_thread(discard, destination)
        return LayoutPoll(status=status, error=error_message(start, status.value)[:2000])

    async def figure(self, operation_url: str, figure_id: str, destination: Path) -> int:
        if not _FIGURE_ID.match(figure_id):
            raise IntegrityError(f"Unexpected DI figure id {figure_id!r}")
        base = self.validate_operation(operation_url).split("?", 1)[0]
        url = f"{base}/figures/{figure_id}?api-version={self._api_version}"
        _, _, size = await self._download(
            "GET", url, destination, max_bytes=self._max_figure_bytes, operation="DI figure"
        )
        return size

    async def delete_result(self, operation_url: str) -> None:
        await self._delete(self.validate_operation(operation_url), operation="DI delete")

    @staticmethod
    def _status(path: Path, size: int) -> LayoutStatus:
        value = status_value(path, size, _STATUS)
        try:
            return LayoutStatus(str(value))
        except ValueError as error:
            raise IntegrityError(f"DI returned an unknown operation status {value!r}") from error
