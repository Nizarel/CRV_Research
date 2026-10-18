"""JSON-lines logging with trace correlation and redaction of content-bearing fields."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # optional: only present with the ``azure`` extra
    _otel_trace = None

REDACTED_KEYS = frozenset(
    {
        "authorization",
        "body",
        "content",
        "markdown",
        "password",
        "sas",
        "signature",
        "text",
        "token",
    }
)
_STANDARD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _trace_ids() -> dict[str, str]:
    if _otel_trace is None:
        return {}
    context = _otel_trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRIBUTES or key.startswith("_"):
                continue
            payload[key] = "[redacted]" if key.lower() in REDACTED_KEYS else value
        payload.update(_trace_ids())
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("azure.core.pipeline.policies.http_logging_policy", "azure.servicebus._pyamqp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
