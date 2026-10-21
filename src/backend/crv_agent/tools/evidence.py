import json

from langchain_core.tools import BaseTool, tool

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.services.retrieval import Retrieval, identifiers


def evidence_tools(
    retrieval: Retrieval, release: str, hashes: list[str], budget: Budget,
    found: dict[str, Evidence], question: str,
) -> list[BaseTool]:
    searches = 0
    reads = 0
    candidates: dict[str, Evidence] = {}
    fixed_identifiers = identifiers(question)

    def expose(hit: Evidence, limit: int) -> dict[str, object]:
        visible = hit.model_copy(update={"content": hit.content[:limit]})
        if hit.id not in found or len(found[hit.id].content) < len(visible.content):
            found[hit.id] = visible
        return {
            **visible.model_dump(mode="json"),
            "truncated": len(hit.content) > limit,
            "total_characters": len(hit.content),
        }

    @tool
    async def search_evidence(query: str) -> str:
        """Search 50 ranked candidates. Read deeper candidates by ID with read_evidence."""
        nonlocal searches
        if searches >= 3:
            raise BudgetExceeded("Worker rewrite allowance exhausted")
        if not 1 <= len(query) <= 4000:
            raise ValueError("Search query must be between 1 and 4000 characters")
        searches += 1
        text = query + "\n" + " ".join(fixed_identifiers)
        hits = await retrieval.retrieve(text.strip(), release, hashes, budget)
        candidates.update((hit.id, hit) for hit in hits)
        return json.dumps({
            "candidates": [
                {
                    "id": hit.id, "title": hit.title, "preview": hit.content[:160],
                    "source_role": hit.source_role,
                } for hit in hits
            ],
            "evidence": [expose(hit, 2500) for hit in hits[:8]],
            "coverage": "Top-50 candidate search, not exhaustive document coverage.",
        }, ensure_ascii=False)

    @tool
    async def read_evidence(evidence_ids: list[str]) -> str:
        """Read up to four candidate IDs, including deep ranks, up to 10000 characters each."""
        nonlocal reads
        if reads >= 3:
            raise BudgetExceeded("Worker evidence-read allowance exhausted")
        if not 1 <= len(evidence_ids) <= 4:
            raise ValueError("Read between one and four evidence IDs")
        if any(key not in candidates for key in evidence_ids):
            raise ValueError("Read requires IDs returned by this worker's searches")
        reads += 1
        return json.dumps([
            expose(candidates[key], 10000) for key in dict.fromkeys(evidence_ids)
        ], ensure_ascii=False)

    return [search_evidence, read_evidence]
