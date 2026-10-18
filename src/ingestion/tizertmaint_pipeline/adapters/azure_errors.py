"""Translate azure-core and HTTP failures into pipeline error categories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..domain.errors import (
    AuthError,
    InvalidInputError,
    PipelineError,
    ThrottledError,
    TransientError,
)

TRANSIENT_STATUS_CODES = frozenset({408, 500, 502, 503, 504})


def parse_retry_after(
    headers: Mapping[str, str] | None, *, now: datetime | None = None
) -> float | None:
    """Seconds to wait from ``retry-after-ms``, ``x-ms-retry-after-ms`` or ``Retry-After``."""

    if not headers:
        return None
    lowered = {key.lower(): value for key, value in headers.items()}
    for key in ("retry-after-ms", "x-ms-retry-after-ms"):
        value = lowered.get(key)
        if value is not None:
            try:
                return max(float(value) / 1000.0, 0.0)
            except ValueError:
                pass
    value = lowered.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - (now or datetime.now(UTC))).total_seconds(), 0.0)


def for_status(
    status_code: int | None, message: str, *, retry_after: float | None
) -> PipelineError:
    if status_code == 429:
        return ThrottledError(message, retry_after=retry_after)
    if status_code in (401, 403):
        return AuthError(message)
    if status_code is None or status_code in TRANSIENT_STATUS_CODES:
        return TransientError(message, retry_after=retry_after)
    if 400 <= status_code < 500:
        return InvalidInputError(message)
    return TransientError(message, retry_after=retry_after)


def translate_azure_error(error: Exception) -> PipelineError | None:
    """Return a classified error for azure-core exceptions, ``None`` for anything else."""

    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    )

    if isinstance(error, ClientAuthenticationError):
        return AuthError(f"Azure authentication failed: {type(error).__name__}")
    if isinstance(error, ServiceRequestError | ServiceResponseError):
        return TransientError(f"Azure transport failure: {type(error).__name__}")
    if isinstance(error, HttpResponseError):
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        status = error.status_code
        service = getattr(error, "error", None)
        detail = getattr(service, "message", None) or error.reason or type(error).__name__
        code = getattr(service, "code", None)
        detail = f"{code}: {detail}" if code else detail
        return for_status(
            status,
            f"Azure request failed with HTTP {status}: {str(detail).splitlines()[0][:300]}",
            retry_after=parse_retry_after(headers),
        )
    return None
