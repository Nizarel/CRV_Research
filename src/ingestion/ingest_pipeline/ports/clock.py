"""Time source port so leases, retries and tests share one notion of 'now'."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current timezone-aware UTC time."""
        ...

    async def sleep(self, seconds: float) -> None: ...
