"""LLM judge: which of a question's key facts the retrieved passages support (Azure OpenAI).

One structured-output call per question and retrieval mode. The judge sees only the passages,
never the expected answer as a whole, and must name the passage that supports each fact.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from ...adapters.azure_ai.structured import structured
from .dataset import GoldenQuestion
from .retrieval import Hit

PASSAGE_CHARS = 1800
_INSTRUCTIONS = (
    "You check retrieval quality for a French mining-research corpus. For each numbered KEY "
    "FACT, decide whether the PASSAGES state it (paraphrase, other number formatting such as "
    "'25,4 %' vs '25.4%', and units written differently are fine; partial support is not). "
    "Use only the passages, never your own knowledge. For a supported fact, give the id of "
    "one passage that states it."
)
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "supported", "passage"],
                "properties": {
                    "index": {"type": "integer"},
                    "supported": {"type": "boolean"},
                    "passage": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def prompt(question: GoldenQuestion, hits: Sequence[Hit]) -> str:
    facts = "\n".join(f"{n}. {fact.text}" for n, fact in enumerate(question.key_facts, 1))
    passages = "\n\n".join(
        f"[{hit.chunk_id}] ({hit.source_path.rsplit('/', 1)[-1]}, p. "
        f"{','.join(str(p) for p in hit.pages) or '?'})\n{hit.content[:PASSAGE_CHARS]}"
        for hit in hits
    )
    return f"QUESTION: {question.question}\n\nKEY FACTS:\n{facts}\n\nPASSAGES:\n{passages}"


class Judge:
    def __init__(self, client: Any, *, deployment: str, concurrency: int = 8) -> None:
        self._client = client
        self._deployment = deployment
        self._gate = asyncio.Semaphore(concurrency)

    async def judge(self, question: GoldenQuestion, hits: Sequence[Hit]) -> list[dict[str, Any]]:
        if not question.key_facts:
            return []
        async with self._gate:
            parsed = await structured(
                self._client,
                self._deployment,
                _INSTRUCTIONS,
                prompt(question, hits),
                "fact_support",
                _SCHEMA,
            )
        verdicts = {int(item["index"]): item for item in parsed.get("facts", [])}
        known = {hit.chunk_id for hit in hits}
        return [
            {
                "fact": n,
                "supported": bool(verdicts.get(n, {}).get("supported")),
                "passage": (verdicts.get(n, {}).get("passage")
                            if verdicts.get(n, {}).get("passage") in known else None),
            }
            for n in range(1, len(question.key_facts) + 1)
        ]
