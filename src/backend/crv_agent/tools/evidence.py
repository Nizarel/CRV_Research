import json

from langchain_core.tools import BaseTool, tool

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.ports.rerank import Reranker
from crv_agent.services.candidates import ranked_candidates
from crv_agent.services.evidence_pack import (
    EvidencePolicy,
    best_window,
    build_pack,
    expose,
    merge_exposure,
)
from crv_agent.services.retrieval import Retrieval, identifiers


def evidence_tools(
    retrieval: Retrieval, release: str, hashes: list[str], budget: Budget,
    found: dict[str, Evidence], question: str,
    policy: EvidencePolicy | None = None, reranker: Reranker | None = None,
) -> list[BaseTool]:
    policy = policy or EvidencePolicy()
    searches = 0
    reads = 0
    candidates: dict[str, tuple[Evidence, dict[str, float]]] = {}
    fixed_identifiers = identifiers(question)

    def show(hit: Evidence, span: tuple[int, int]) -> dict[str, object]:
        visible = expose(hit, span)
        found[hit.id] = merge_exposure(found[hit.id], visible) if hit.id in found else visible
        return {
            "id": hit.id, "title": hit.title, "source": hit.source_uri, "pages": hit.pages,
            "locators": hit.locators, "document_class": hit.document_class,
            "citation_basis": hit.citation_basis, "source_role": hit.source_role,
            "excerpt": list(span), "total_characters": len(hit.content),
            "truncated": span != (0, len(hit.content)), "text": hit.content[span[0]:span[1]],
        }

    @tool
    async def search_evidence(query: str) -> str:
        """Search 50 ranked candidates; returns focused excerpts and previews of the rest."""
        nonlocal searches
        if searches >= 3:
            raise BudgetExceeded("Worker rewrite allowance exhausted")
        if not 1 <= len(query) <= 4000:
            raise ValueError("Search query must be between 1 and 4000 characters")
        searches += 1
        text = (query + "\n" + " ".join(fixed_identifiers)).strip()
        hits = await ranked_candidates(retrieval, text, release, hashes, budget, policy, reranker)
        pack = build_pack(hits, text, fixed_identifiers, policy)
        candidates.update(
            (hit.id, (hit, pack.weights))
            for hit in [*(h for h, _ in pack.shown), *(h for h, _ in pack.previews)]
        )
        return json.dumps({
            "evidence": [show(hit, span) for hit, span in pack.shown],
            "more_candidates": [
                {
                    "id": hit.id, "title": hit.title, "source_role": hit.source_role,
                    "preview": preview,
                } for hit, preview in pack.previews
            ],
            "coverage": "Top-50 candidate search, not exhaustive document coverage. "
            "Previews are not evidence.",
        }, ensure_ascii=False)

    @tool
    async def read_evidence(evidence_ids: list[str]) -> str:
        """Read up to four candidate IDs in full (bounded length per candidate)."""
        nonlocal reads
        if reads >= 3:
            raise BudgetExceeded("Worker evidence-read allowance exhausted")
        if not 1 <= len(evidence_ids) <= 4:
            raise ValueError("Read between one and four evidence IDs")
        if any(key not in candidates for key in evidence_ids):
            raise ValueError("Read requires IDs returned by this worker's searches")
        reads += 1
        result = []
        for key in dict.fromkeys(evidence_ids):
            hit, weights = candidates[key]
            result.append(show(hit, best_window(hit.content, weights, policy.read_characters)))
        return json.dumps(result, ensure_ascii=False)

    return [search_evidence, read_evidence]

