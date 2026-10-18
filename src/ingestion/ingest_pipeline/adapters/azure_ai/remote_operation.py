"""Shared raw-REST plumbing for durable remote analysis operations (DI and CU).

SDK pollers are not used: the Operation-Location must be persisted the moment a request is
accepted, request bodies are streamed from disk, and results (possibly hundreds of MB) are
streamed to disk. SDK retries are off because the stage ledger owns the retry budget.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ...domain.errors import (
    ConfigurationError,
    IntegrityError,
    PipelineError,
    RemoteResultMissingError,
    ResourceExhaustedError,
    SubmissionUnknownError,
    TransientError,
)
from ..azure_errors import for_status, parse_retry_after

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
API_KEY_HEADER = "Ocp-Apim-Subscription-Key"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
STATUS_PROBE_BYTES = 64 * 1024
SMALL_BODY_BYTES = 16 * 1024 * 1024


class ApiKeyFile:
    """An API key mounted as a file (a Key Vault secret); re-read when the file changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: int | None = None
        self._key = ""

    @property
    def path(self) -> Path:
        return self._path

    @property
    def key(self) -> str:
        stamp = self._path.stat().st_mtime_ns
        if stamp != self._stamp:
            key = self._path.read_text(encoding="utf-8").strip()
            if not key:
                raise ConfigurationError(f"API key file {self._path} is empty")
            self._key, self._stamp = key, stamp
        return self._key


def discard(path: Path) -> None:
    path.unlink(missing_ok=True)


def loaded_body(response: Any) -> bytes:
    """Body of a non-streamed response (already read by the pipeline)."""

    try:
        return bytes(response.content or b"")
    except Exception:
        return b""


def head(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def error_message(body: bytes, fallback: str) -> str:
    try:
        error = json.loads(body.decode("utf-8")).get("error") or {}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return fallback
    inner = error.get("innererror") or {}
    parts = [error.get("code"), inner.get("code"), error.get("message")]
    return ": ".join(str(part) for part in parts if part) or fallback


def header(headers: dict[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


def status_value(path: Path, size: int, pattern: re.Pattern[bytes]) -> str | None:
    """The operation ``status`` from the head of a (possibly huge) stored response."""

    with path.open("rb") as handle:
        start = handle.read(STATUS_PROBE_BYTES)
    match = pattern.search(start)
    if match is not None:
        return match.group(1).decode("ascii")
    if size <= SMALL_BODY_BYTES:
        document = json.loads(path.read_bytes())
        value = document.get("status") if isinstance(document, dict) else None
        return None if value is None else str(value)
    return None


class RemoteOperationClient:
    """azure-core pipeline bound to one https resource; credentials only ever go to that host.

    ``credential`` is a token credential (workload identity) or an ``ApiKeyFile``.
    """

    service = "remote analysis"

    def __init__(
        self,
        *,
        endpoint: str,
        credential: Any | None,
        api_version: str,
        transport: Any | None = None,
        user_agent: str = "tizert-pipeline",
    ) -> None:
        from azure.core import AsyncPipelineClient
        from azure.core.pipeline.policies import (
            AsyncBearerTokenCredentialPolicy,
            AzureKeyCredentialPolicy,
            HeadersPolicy,
            RequestIdPolicy,
            UserAgentPolicy,
        )

        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower()
        if not host or (parts.scheme != "https" and host not in LOCAL_HOSTS):
            raise ConfigurationError(f"The {self.service} endpoint must be https")
        self._scheme = parts.scheme
        self._netloc = parts.netloc.lower()
        self._host = host
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._api_version = api_version
        policies: list[Any] = [RequestIdPolicy(), HeadersPolicy(), UserAgentPolicy(user_agent)]
        if isinstance(credential, ApiKeyFile):
            policies.append(AzureKeyCredentialPolicy(credential, API_KEY_HEADER))  # type: ignore[arg-type]
        elif credential is not None:
            policies.append(
                AsyncBearerTokenCredentialPolicy(credential, COGNITIVE_SERVICES_SCOPE)
            )
        self._client: Any = AsyncPipelineClient(
            base_url=self._base, policies=policies, transport=transport
        )

    @property
    def api_version(self) -> str:
        return self._api_version

    @property
    def endpoint_host(self) -> str:
        return self._host

    async def close(self) -> None:
        await self._client.close()

    def _same_resource(self, url: str) -> tuple[str, dict[str, str]]:
        parts = urlsplit(url)
        if parts.scheme != self._scheme or parts.netloc.lower() != self._netloc or parts.fragment:
            raise ConfigurationError(f"Untrusted or unexpected {self.service} Operation-Location")
        return parts.path, dict(parse_qsl(parts.query, keep_blank_values=True))

    async def _accepted(
        self, url: str, *, headers: dict[str, str], content: AsyncIterable[bytes], operation: str
    ) -> dict[str, str]:
        """POST a streamed body; return the 202 headers or raise a classified error."""

        from azure.core.exceptions import (
            HttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
        )
        from azure.core.rest import HttpRequest

        request = HttpRequest("POST", url, headers=headers, content=content)
        try:
            response = await self._client.send_request(request)
        except ServiceRequestError as error:
            raise TransientError(
                f"{operation} request was not sent: {type(error).__name__}"
            ) from error
        except (ServiceResponseError, HttpResponseError) as error:
            raise SubmissionUnknownError(
                f"{operation} outcome unknown after sending: {type(error).__name__}"
            ) from error
        try:
            body = loaded_body(response)
        finally:
            await response.close()
        response_headers = dict(response.headers)
        if response.status_code != 202:
            raise self._http_error(response.status_code, response_headers, body, operation)
        return response_headers

    async def _send(self, request: Any, *, stream: bool, operation: str) -> Any:
        from azure.core.exceptions import ServiceRequestError, ServiceResponseError

        try:
            return await self._client.send_request(request, stream=stream)
        except (ServiceRequestError, ServiceResponseError) as error:
            raise TransientError(
                f"{operation} transport failure: {type(error).__name__}"
            ) from error

    async def _delete(self, url: str, *, operation: str) -> None:
        from azure.core.rest import HttpRequest

        response = await self._send(HttpRequest("DELETE", url), stream=False, operation=operation)
        try:
            body = loaded_body(response)
        finally:
            await response.close()
        if response.status_code not in (200, 204, 404):
            raise self._http_error(response.status_code, dict(response.headers), body, operation)

    async def _get_json(self, url: str, *, operation: str, max_bytes: int) -> dict[str, Any]:
        from azure.core.rest import HttpRequest

        response = await self._send(HttpRequest("GET", url), stream=False, operation=operation)
        try:
            body = loaded_body(response)
        finally:
            await response.close()
        if response.status_code != 200:
            raise self._http_error(response.status_code, dict(response.headers), body, operation)
        if len(body) > max_bytes:
            raise ResourceExhaustedError(f"{operation}: response exceeds {max_bytes} bytes")
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict):
            raise IntegrityError(f"{operation} returned a non-object body")
        return document

    async def _download(
        self, method: str, url: str, destination: Path, *, max_bytes: int, operation: str
    ) -> tuple[int, dict[str, str], int]:
        from azure.core.rest import HttpRequest

        response = await self._send(HttpRequest(method, url), stream=True, operation=operation)
        size = 0
        try:
            headers = dict(response.headers)
            if response.status_code == 404:
                raise RemoteResultMissingError(f"{operation}: the remote result no longer exists")
            if response.status_code != 200:
                body = await response.read()
                raise self._http_error(response.status_code, headers, body, operation)
            handle = await asyncio.to_thread(destination.open, "wb")
            with handle:
                async for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ResourceExhaustedError(
                            f"{operation}: response exceeds {max_bytes} bytes"
                        )
                    handle.write(chunk)
        except BaseException:
            discard(destination)
            raise
        finally:
            await response.close()
        return response.status_code, headers, size

    @staticmethod
    def _http_error(
        status_code: int, headers: dict[str, str], body: bytes, operation: str
    ) -> PipelineError:
        message = error_message(body, f"HTTP {status_code}")
        return for_status(
            status_code,
            f"{operation} failed with HTTP {status_code}: {message}"[:2000],
            retry_after=parse_retry_after(headers),
        )
