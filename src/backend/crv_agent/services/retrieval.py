import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from typing import Literal
from urllib.parse import unquote

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.execution import Budget, BudgetExceeded
from crv_agent.ports.retrieval import Search

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(
    r"\b[A-Za-z]{1,6}(?:[-/][A-Za-z0-9]{1,10}){2,7}\b|\b[A-Z]{1,5}\d{3,8}\b",
)
CANDIDATE_DEPTH = 50
_TABLE_REFERENCE = re.compile(r"\b(?:tableaux?|tables?)\b", re.IGNORECASE)
_TABLE_SEEDS = 5
_TABLE_ADDITIONS = 8
_PERIODIC = re.compile(
    r"\brapport d activite\b|\brapport mensuel\b|\bmonthly report\b|^ra\b"
    r"|\bexecutive summary\b|\bflash\b|^reporting\b",
)


def identifiers(text: str) -> list[str]:
    return list(dict.fromkeys(
        value for value in _IDENTIFIER.findall(text) if any(char.isdigit() for char in value)
    ))


class Retrieval:
    def __init__(self, search: Search) -> None:
        self.search = search

    async def retrieve(
        self, text: str, release: str, hashes: list[str], budget: Budget,
        captions: bool = False, *, strict: bool = False,
    ) -> list[Evidence]:
        """Fused candidates; ``hashes`` add a scoped leg, or with ``strict`` are the only scope."""
        only = strict and bool(hashes)
        modes: list[tuple[Literal["lexical", "semantic"], list[str]]] = (
            [] if only else [("semantic", [])]
        )
        if hashes:
            modes.append(("semantic", hashes))
        if any(c.isdigit() for c in text):
            modes.append(("lexical", hashes if only else []))
        await budget.reserve(retrieval=len(modes))
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(budget.wait(self.search.query(
                    text, release, mode, selected, top=CANDIDATE_DEPTH,
                    captions=captions and mode == "semantic",
                )))
                for mode, selected in modes
            ]
        hits = focus(fuse([task.result() for task in tasks]))
        return await self.with_tables(hits, release, budget)

    async def with_tables(
        self, hits: list[Evidence], release: str, budget: Budget,
    ) -> list[Evidence]:
        seeds: list[Evidence] = []
        scopes: dict[str, list[int]] = {}
        for hit in hits:
            if (
                hit.release != release or hit.generated or not hit.pages
                or periodic_summary(hit) or not _TABLE_REFERENCE.search(hit.content)
            ):
                continue
            if not re.fullmatch(r"[a-f0-9]{64}", hit.content_hash):
                continue
            neighbors = sorted({
                page + offset for page in hit.pages[:3] for offset in (-1, 0, 1)
                if page + offset > 0
            })
            known = scopes.setdefault(hit.content_hash, [])
            if set(neighbors) <= set(known):
                continue
            scopes[hit.content_hash] = sorted(set(known + neighbors))[:15]
            seeds.append(hit)
            if len(seeds) == _TABLE_SEEDS:
                break
        if not seeds:
            return hits
        try:
            await budget.reserve(retrieval=1)
            tables = await budget.wait(self.search.tables(release, scopes))
        except BudgetExceeded:
            return hits
        except Exception as error:
            logger.warning("table_lookup_failed error_type=%s", type(error).__name__)
            return hits
        return insert_tables(hits, seeds, tables, release)


def insert_tables(
    hits: list[Evidence], seeds: list[Evidence], tables: list[Evidence], release: str,
) -> list[Evidence]:
    existing = {hit.id for hit in hits}
    seed_ids = {seed.id for seed in seeds}
    result: list[Evidence] = []
    added = 0
    for hit in hits:
        result.append(hit)
        if hit.id not in seed_ids:
            continue
        neighbors = [
            table for table in tables
            if table.id not in existing and table.release == release and not table.generated
            and table.content_hash == hit.content_hash
            and table.pages and any(abs(page - anchor) <= 1
                                    for page in table.pages for anchor in hit.pages[:3])
        ]
        neighbors.sort(key=lambda table: (
            min(abs(page - anchor) for page in table.pages for anchor in hit.pages[:3]),
            min(table.pages), table.id,
        ))
        for table in neighbors[:min(2, _TABLE_ADDITIONS - added)]:
            result.append(table)
            existing.add(table.id)
            added += 1
    return result[:CANDIDATE_DEPTH]


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
