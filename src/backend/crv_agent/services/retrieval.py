import asyncio
import re
import unicodedata
from collections import defaultdict
from typing import Literal
from urllib.parse import unquote

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget
from crv_agent.ports.retrieval import Search

_IDENTIFIER = re.compile(
    r"\b[A-Za-z]{1,6}(?:[-/][A-Za-z0-9]{1,10}){2,7}\b|\b[A-Z]{1,5}\d{3,8}\b",
)
CANDIDATE_DEPTH = 50
_PERIODIC = re.compile(
    r"\brapport d activite\b|\brapport mensuel\b|\bmonthly report\b|^ra\b"
    r"|\bexecutive summary\b|\bflash\b|^reporting\b",
)


def identifiers(text: str) -> list[str]:
    return list(dict.fromkeys(_IDENTIFIER.findall(text)))


class Retrieval:
    def __init__(self, search: Search) -> None:
        self.search = search

    async def retrieve(
        self, text: str, release: str, hashes: list[str], budget: Budget,
        captions: bool = False,
    ) -> list[Evidence]:
        modes: list[tuple[Literal["lexical", "semantic"], list[str]]] = [("semantic", [])]
        if hashes:
            modes.append(("semantic", hashes))
        if any(c.isdigit() for c in text):
            modes.append(("lexical", []))
        await budget.reserve(retrieval=len(modes))
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(budget.wait(self.search.query(
                    text, release, mode, selected, top=CANDIDATE_DEPTH,
                    captions=captions and mode == "semantic",
                )))
                for mode, selected in modes
            ]
        return focus(fuse([task.result() for task in tasks]))


def fuse(rankings: list[list[Evidence]], top: int = CANDIDATE_DEPTH) -> list[Evidence]:
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, Evidence] = {}
    for ranking in rankings:
        for rank, evidence in enumerate(ranking, 1):
            if evidence.generated:
                continue
            scores[evidence.id] += 1 / (60 + rank)
            # Keep the first occurrence: semantic legs come first and carry captions.
            by_id.setdefault(evidence.id, evidence)
    return [by_id[key] for key in sorted(scores, key=lambda k: (-scores[k], k))[:top]]


def periodic_summary(evidence: Evidence) -> bool:
    # Mirror golden-eval is_periodic(): the original corpus filename decides; source_uri is a
    # content-addressed blob path. Fall back to the title (filename + page) only without a path.
    names = (
        [evidence.source_path] if evidence.source_path
        else [evidence.title, evidence.source_uri]
    )
    for uri in names:
        name = unquote(uri).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        folded = unicodedata.normalize("NFKD", name.casefold())
        words = " ".join(re.findall(
            r"[a-z0-9]+", "".join(c for c in folded if not unicodedata.combining(c)),
        ))
        if _PERIODIC.search(words):
            return True
    return False


def focus(hits: list[Evidence]) -> list[Evidence]:
    """Apply the measured soft rank penalties; never exclude a summary or repeated chunk."""
    seen: dict[str, int] = defaultdict(int)
    ranked: list[tuple[int, int, Evidence]] = []
    for rank, hit in enumerate(hits, 1):
        key = hit.content_hash or hit.id
        seen[key] += 1
        secondary = periodic_summary(hit)
        if secondary:
            hit = hit.model_copy(update={
                "source_role": "secondary_summary", "source_role_basis": "filename",
            })
        penalty = (10 if secondary else 0) + (20 if seen[key] > 2 else 0)
        ranked.append((rank + penalty, rank, hit))
    return [hit for _, _, hit in sorted(ranked, key=lambda item: (item[0], item[1]))]
