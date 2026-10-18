"""Order-preserving packing of items into size- and count-bounded batches."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PackResult:
    batches: tuple[tuple[int, ...], ...]
    """Batches of item indexes, in input order."""

    oversized: tuple[int, ...]
    """Indexes whose own size exceeds ``max_item`` or ``max_total``; never sent."""


def pack(
    sizes: Sequence[int],
    *,
    max_items: int,
    max_total: int,
    max_item: int | None = None,
) -> PackResult:
    """Greedily fill batches without exceeding ``max_items`` items or ``max_total`` size."""

    if max_items < 1 or max_total < 1:
        raise ValueError("Batch limits must be positive")
    item_cap = min(max_item, max_total) if max_item is not None else max_total

    batches: list[tuple[int, ...]] = []
    oversized: list[int] = []
    current: list[int] = []
    current_total = 0
    for index, size in enumerate(sizes):
        if size < 0:
            raise ValueError("Item sizes must be non-negative")
        if size > item_cap:
            oversized.append(index)
            continue
        if current and (len(current) >= max_items or current_total + size > max_total):
            batches.append(tuple(current))
            current, current_total = [], 0
        current.append(index)
        current_total += size
    if current:
        batches.append(tuple(current))
    return PackResult(batches=tuple(batches), oversized=tuple(oversized))
