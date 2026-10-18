"""Error taxonomy shared by stages, adapters and the retry policy."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    THROTTLED = "throttled"
    SLOT_WAIT = "slot_wait"
    """Parked until a remote-operation slot frees; woken by the ledger, not by a timer."""
    AUTH = "auth"
    CONFIGURATION = "configuration"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported"
    STALE_SOURCE = "stale_source"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    SUBMISSION_UNKNOWN = "submission_unknown"
    INTEGRITY = "integrity"
    LEASE_EXPIRED = "lease_expired"
    TIMEOUT = "timeout"
    BUG = "bug"


class PipelineError(Exception):
    """Base class for classified failures; ``retry_after`` is in seconds."""

    category: ErrorCategory = ErrorCategory.BUG

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(PipelineError):
    category = ErrorCategory.TRANSIENT


class ThrottledError(PipelineError):
    category = ErrorCategory.THROTTLED


class AuthError(PipelineError):
    category = ErrorCategory.AUTH


class ConfigurationError(PipelineError):
    category = ErrorCategory.CONFIGURATION


class InvalidInputError(PipelineError):
    category = ErrorCategory.INVALID_INPUT


class UnsupportedContentError(PipelineError):
    category = ErrorCategory.UNSUPPORTED


class StaleSourceError(PipelineError):
    category = ErrorCategory.STALE_SOURCE


class ResourceExhaustedError(PipelineError):
    category = ErrorCategory.RESOURCE_EXHAUSTED


class SubmissionUnknownError(PipelineError):
    category = ErrorCategory.SUBMISSION_UNKNOWN


class RemoteResultMissingError(PipelineError):
    """A provider operation or result no longer exists (expired or deleted)."""

    category = ErrorCategory.INVALID_INPUT


class IntegrityError(PipelineError):
    """Stored or returned data does not match its declared digest, size or shape."""

    category = ErrorCategory.INTEGRITY


class LeaseLostError(PipelineError):
    """A newer lease (higher fencing token) owns the job; this worker must not commit."""

    category = ErrorCategory.LEASE_EXPIRED


RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "57014"})
"""PostgreSQL serialization failure, deadlock and statement timeout: the statement can rerun."""


def classify(error: BaseException) -> tuple[ErrorCategory, float | None]:
    """Map an exception to a category and optional server-provided retry delay."""

    if isinstance(error, PipelineError):
        return error.category, error.retry_after
    if getattr(error, "sqlstate", None) in RETRYABLE_SQLSTATES:
        return ErrorCategory.TRANSIENT, None
    if isinstance(error, TimeoutError):
        return ErrorCategory.TIMEOUT, None
    if isinstance(error, ConnectionError | OSError):
        return ErrorCategory.TRANSIENT, None
    if isinstance(error, MemoryError):
        return ErrorCategory.RESOURCE_EXHAUSTED, None
    return ErrorCategory.BUG, None
