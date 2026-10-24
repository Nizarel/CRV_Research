"""KG-guided retrieval (design §8.5): released KG-2 studies choose the documents of one search.

The facet asks the user's own question, searched only inside the documents that report studies
of the resolved site or report code; the graph contributes document choice, never claims.
"""

from crv_agent.domain.planning import StudyRef

KG_INTENTS = frozenset({"synthesis", "timeline", "comparison"})
MAX_DOCUMENTS = 8


def kg_documents(studies: list[StudyRef]) -> list[str]:
    """Distinct study documents in period order, evenly spread when there are too many."""
    ordered = list(dict.fromkeys(study.content_hash for study in studies if study.content_hash))
    if len(ordered) <= MAX_DOCUMENTS:
        return ordered
    # Even spacing keeps a long timeline's earliest and latest reports.
    step = (len(ordered) - 1) / (MAX_DOCUMENTS - 1)
    return [ordered[round(i * step)] for i in range(MAX_DOCUMENTS)]
