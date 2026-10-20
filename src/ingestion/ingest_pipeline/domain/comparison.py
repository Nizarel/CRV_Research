"""Compare two extraction arms over the same contents: coverage, structure, completeness, cost.

Pure functions over normalized evidence; the CLI loads the evidence and prints the report.
Coverage is a proxy, not a quality verdict: the reviewer sample is what decides.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .evidence import BlockKind, EvidenceDocument, EvidenceUnit, PageStatus


@dataclass
class ArmStats:
    contents: int = 0
    complete: int = 0
    pages: int = 0
    pages_with_text: int = 0
    characters: int = 0
    tables: int = 0
    table_cells: int = 0
    figures: int = 0
    failed_units: int = 0
    missing: int = 0
    page_chars: dict[tuple[str, int], int] = field(default_factory=dict)


def collect(
    stats: ArmStats, content_hash: str, document: EvidenceDocument | None,
    units: Sequence[EvidenceUnit],
) -> None:
    if document is None:
        stats.missing += 1
        return
    stats.contents += 1
    stats.complete += int(document.complete)
    stats.failed_units += sum(
        1 for unit in document.units
        if unit.disposition in ("failed", "unsupported", "quarantined")
    )
    chars: dict[int, int] = {
        page.page: 0 for page in document.pages if page.status is not PageStatus.MISSING
    }
    for unit in units:
        for block in unit.blocks:
            size = len(block.text)
            if block.kind is BlockKind.TABLE and block.table is not None:
                stats.tables += 1
                stats.table_cells += len(block.table.cells)
                size = sum(len(cell.text) for cell in block.table.cells)
            elif block.kind is BlockKind.FIGURE:
                stats.figures += 1
            stats.characters += size
            for page in block.pages[:1]:
                chars[page] = chars.get(page, 0) + size
    stats.pages += len(chars)
    stats.pages_with_text += sum(1 for value in chars.values() if value)
    stats.page_chars.update({(content_hash, page): value for page, value in chars.items()})


def summary(stats: ArmStats, *, pages_billed: int, page_usd: float) -> dict[str, Any]:
    return {
        "contents": stats.contents,
        "complete_documents": stats.complete,
        "missing_evidence": stats.missing,
        "failed_or_unsupported_units": stats.failed_units,
        "pages": stats.pages,
        "pages_with_text": stats.pages_with_text,
        "characters": stats.characters,
        "tables": stats.tables,
        "table_cells": stats.table_cells,
        "figures": stats.figures,
        "billed_pages": pages_billed,
        "estimated_usd": round(pages_billed * page_usd, 2),
    }


def divergent_pages(
    left: ArmStats, right: ArmStats, *, ratio: float = 0.5
) -> list[tuple[str, int, int, int]]:
    """Pages where one arm extracted far less text than the other (both arms evaluated)."""

    found = []
    for key in sorted(set(left.page_chars) & set(right.page_chars)):
        a, b = left.page_chars[key], right.page_chars[key]
        if max(a, b) and min(a, b) < ratio * max(a, b):
            found.append((key[0], key[1], a, b))
    return found


def review_sample(
    left: ArmStats, right: ArmStats, labels: Mapping[str, str], *, size: int, seed: int
) -> list[dict[str, Any]]:
    """Deterministic reviewer pages: every divergent page first, then a seeded random fill."""

    divergent = divergent_pages(left, right)
    chosen = [(content, page) for content, page, _, _ in divergent][:size]
    rest = sorted((set(left.page_chars) & set(right.page_chars)) - set(chosen))
    random.Random(seed).shuffle(rest)
    chosen += rest[: max(0, size - len(chosen))]
    return [
        {
            "content_hash": content, "source": labels.get(content, content), "page": page,
            "chars_a": left.page_chars[(content, page)],
            "chars_b": right.page_chars[(content, page)],
            "divergent": (content, page) in {(c, p) for c, p, _, _ in divergent},
        }
        for content, page in chosen
    ]
