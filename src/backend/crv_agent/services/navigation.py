"""KG navigation selection, shared by the research graph and retrieval evaluation."""

from crv_agent.domain.planning import Entity

PRECISE_KINDS = frozenset({"Site", "ReportCode"})
NAVIGABLE_STATUSES = frozenset({"observed", "candidate", "approved"})


def steering_entities(entities: list[Entity], intent: str) -> list[Entity]:
    """Catalogue requests navigate broadly; research boosts use precise entities only.

    Cover-page Projects are too broad to steer retrieval, and Organisations steer only once
    approved (measured: docs/evaluation/golden-retrieval-baseline-2026-10-21.md).
    """
    if intent == "catalogue":
        return entities
    return [
        e for e in entities
        if (e.kind in PRECISE_KINDS and e.status in NAVIGABLE_STATUSES)
        or (e.kind == "Organisation" and e.status == "approved")
    ]
