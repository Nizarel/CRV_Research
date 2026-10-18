"""Deterministic, manually advanced clock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta


class ManualClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.utcoffset() is None:
            raise ValueError("ManualClock requires a timezone-aware start")

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
