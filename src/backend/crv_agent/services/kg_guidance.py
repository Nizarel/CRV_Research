"""KG-guided retrieval (design §8.5): released KG-2 observations steer one extra facet.

The facet asks the evidence worker to find and quote the reported values in their source
documents, restricted to those documents; the graph values themselves are never claims.
"""

from collections import defaultdict
from itertools import zip_longest

from crv_agent.domain.planning import Facet, ObservationRef

KG_INTENTS = frozenset({"synthesis", "timeline", "comparison"})
MAX_VALUES = 12


def kg_facet(observations: list[ObservationRef]) -> tuple[Facet, list[str]] | None:
    """One facet listing reported values (round-robin across documents) and their hashes."""
    by_document: dict[str, list[ObservationRef]] = defaultdict(list)
    for item in observations:
        if item.content_hash:
            by_document[item.content_hash].append(item)
    picked = [
        item for row in zip_longest(*by_document.values()) for item in row if item is not None
    ][:MAX_VALUES]
    if not picked:
        return None
    values = "; ".join(
        " ".join(part for part in (
            item.property, item.element, item.stream, item.value_text,
            f"({', '.join(p for p in (item.study or item.subject, item.period) if p)})"
            if item.study or item.subject or item.period else None,
        ) if part)
        for item in picked
    )
    hashes = list(dict.fromkeys(item.content_hash for item in picked if item.content_hash))
    question = f"Locate and quote these reported results in their source documents: {values}"
    return Facet(question=question[:4000]), hashes
