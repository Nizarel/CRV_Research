from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget
from crv_agent.ports.rerank import Reranker
from crv_agent.services.evidence_pack import (
    EvidencePolicy,
    apply_ranking,
    best_window,
    term_weights,
)
from crv_agent.services.retrieval import Retrieval

RERANK_PREVIEW_CHARACTERS = 600


async def ranked_candidates(
    retrieval: Retrieval, query: str, release: str, hashes: list[str], budget: Budget,
    policy: EvidencePolicy, reranker: Reranker | None, *, strict: bool = False,
) -> list[Evidence]:
    """Focused Search candidates, optionally reordered by a listwise model reranker."""
    hits = await retrieval.retrieve(
        query, release, hashes, budget, captions=policy.anchor == "caption", strict=strict,
    )
    if policy.rerank == "llm":
        if reranker is None:
            raise ValueError("The llm rerank policy needs a reranker")
        if len(hits) > 1:
            weights = term_weights(query, hits, [])
            ranked = await reranker.rerank(query, [
                (hit.id, hit.title, hit.content[
                    slice(*best_window(hit.content, weights, RERANK_PREVIEW_CHARACTERS))
                ]) for hit in hits
            ], budget)
            hits = apply_ranking(hits, ranked)
    return hits
