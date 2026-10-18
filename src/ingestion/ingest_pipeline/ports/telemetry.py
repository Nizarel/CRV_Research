"""Telemetry port: spans and stage metrics without leaking document content."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class Telemetry(Protocol):
    def span(self, name: str, **attributes: Any) -> AbstractContextManager[None]: ...

    def stage_finished(
        self,
        *,
        stage: str,
        status: str,
        duration_seconds: float,
        error_category: str | None,
    ) -> None: ...

    def counter(self, name: str, value: int = 1, **attributes: Any) -> None: ...
