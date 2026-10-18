"""OpenTelemetry traces and stage metrics, exported to Azure Monitor when configured."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any

from ...config import TelemetrySettings
from ...ports.telemetry import Telemetry
from .json_logging import configure_logging

logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "tizertmaint_pipeline"
CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def _attributes(values: dict[str, Any]) -> dict[str, str | int | float | bool]:
    clean: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        clean[key] = value if isinstance(value, str | int | float | bool) else str(value)
    return clean


class NoopTelemetry:
    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        yield

    def stage_finished(
        self, *, stage: str, status: str, duration_seconds: float, error_category: str | None
    ) -> None:
        return None

    def counter(self, name: str, value: int = 1, **attributes: Any) -> None:
        return None


class OtelTelemetry:
    def __init__(self) -> None:
        from opentelemetry import metrics, trace

        self._tracer = trace.get_tracer(INSTRUMENTATION_NAME)
        self._meter = metrics.get_meter(INSTRUMENTATION_NAME)
        self._duration = self._meter.create_histogram(
            "pipeline.stage.duration", unit="s", description="Stage execution duration"
        )
        self._outcomes = self._meter.create_counter(
            "pipeline.stage.outcomes", description="Stage executions by outcome"
        )
        self._counters: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        with self._tracer.start_as_current_span(name, attributes=_attributes(attributes)):
            yield

    def stage_finished(
        self, *, stage: str, status: str, duration_seconds: float, error_category: str | None
    ) -> None:
        attributes = _attributes(
            {"stage": stage, "status": status, "error_category": error_category}
        )
        self._duration.record(duration_seconds, attributes)
        self._outcomes.add(1, attributes)

    def counter(self, name: str, value: int = 1, **attributes: Any) -> None:
        counter = self._counters.get(name)
        if counter is None:
            counter = self._counters[name] = self._meter.create_counter(name)
        counter.add(value, _attributes(attributes))


def configure_telemetry(settings: TelemetrySettings) -> Telemetry:
    """Configure JSON logs; export traces/metrics/logs to Azure Monitor if a connection exists."""

    configure_logging(settings.log_level, json_logs=settings.json_logs)
    if not os.environ.get(CONNECTION_STRING_ENV):
        logger.info("Azure Monitor export disabled: %s is not set", CONNECTION_STRING_ENV)
        return NoopTelemetry()
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logger.warning("azure-monitor-opentelemetry is not installed; telemetry disabled")
        return NoopTelemetry()

    os.environ.setdefault("OTEL_SERVICE_NAME", settings.service_name)
    configure_azure_monitor(logger_name=INSTRUMENTATION_NAME)
    return OtelTelemetry()
