"""In-memory telemetry recorder for tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordingTelemetry:
    spans: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    counters: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        self.spans.append((name, attributes))
        yield

    def stage_finished(
        self, *, stage: str, status: str, duration_seconds: float, error_category: str | None
    ) -> None:
        self.stages.append(
            {
                "stage": stage,
                "status": status,
                "duration_seconds": duration_seconds,
                "error_category": error_category,
            }
        )

    def counter(self, name: str, value: int = 1, **attributes: Any) -> None:
        self.counters.append((name, value, attributes))
