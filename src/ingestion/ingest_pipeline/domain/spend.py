"""List-price cost of recorded provider usage, used to fill billing accounts in order."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import ProviderUsage


@dataclass(frozen=True)
class Prices:
    di_page_usd: float
    cu_page_estimate_usd: float
    """Reserved per in-flight CU page, and the floor when a usage meter has no price."""
    cu_meter_usd: Mapping[str, float] = field(default_factory=dict)
    """USD per unit of each CU usage meter (``documentPagesStandard``, ``contextualizationTokens``,
    ``<model>-input``, ``<model>-cached-input``, ``<model>-output``)."""

    def di_cost(self, pages: int) -> float:
        return pages * self.di_page_usd

    def cu_cost(self, usage: ProviderUsage) -> tuple[float, list[str]]:
        """Cost of one CU operation and the meters it reported without a price."""

        details: Mapping[str, Any] = usage.details
        if "meters" not in details:
            return usage.pages * self.cu_page_estimate_usd, []
        units: dict[str, int] = {
            **{str(key): int(value) for key, value in (details.get("meters") or {}).items()},
            "contextualizationTokens": int(details.get("contextualization_tokens") or 0),
            **{str(key): int(value) for key, value in (details.get("tokens") or {}).items()},
        }
        unpriced = sorted(
            key for key, count in units.items() if count and key not in self.cu_meter_usd
        )
        cost = sum(count * self.cu_meter_usd.get(key, 0.0) for key, count in units.items())
        if unpriced:
            cost = max(cost, usage.pages * self.cu_page_estimate_usd)
        return cost, unpriced
