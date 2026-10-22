"""Deterministic per-facet evidence packing over the focused Search candidates.

Order follows the measured focused ranking. Only exact identifier matches move up.
Each packed chunk exposes one contiguous, query-focused excerpt; claims may quote only
exposed spans.
"""

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from crv_agent.domain.evidence import Evidence

PACK_SIZE = 16
EXCERPT_CHARACTERS = 1200
READ_CHARACTERS = 8000
PREVIEW_CHARACTERS = 160
_WORD = re.compile(r"\w+")
_STOPWORDS = frozenset("""
les des une dans pour par sur avec aux est sont qui que quel quelle quels quelles
cette ces son ses leur leurs entre selon plus moins comment pourquoi quand
the and for with from that this which what when were was are into over under
""".split())


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True)
class EvidencePolicy:
    """Evidence exposure for one facet; defaults are the measured runtime configuration."""

    pack_size: int = PACK_SIZE
    excerpt_characters: int = EXCERPT_CHARACTERS
    read_characters: int = READ_CHARACTERS
    anchor: Literal["lexical", "caption"] = "lexical"
    rerank: Literal["none", "llm"] = "none"

    def __post_init__(self) -> None:
        if not 1 <= self.pack_size <= 50:
            raise ValueError("pack_size must be between 1 and 50")
        if not 200 <= self.excerpt_characters <= self.read_characters <= 20_000:
            raise ValueError("Need 200 <= excerpt_characters <= read_characters <= 20000")


@dataclass(frozen=True)
class Pack:
    shown: list[tuple[Evidence, tuple[int, int]]]
    previews: list[tuple[Evidence, str]]
    weights: dict[str, float]


def query_terms(text: str) -> set[str]:
    return {
        term for term in (fold(word) for word in _WORD.findall(text))
        if term not in _STOPWORDS and (len(term) >= 3 or any(c.isdigit() for c in term))
    }


def term_weights(query: str, hits: list[Evidence], identifiers: list[str]) -> dict[str, float]:
    terms = query_terms(query)
    documents = [set(fold(word) for word in _WORD.findall(hit.content)) for hit in hits]
    weights = {
        term: math.log(1 + (len(documents) + 1) / (1 + sum(term in d for d in documents)))
        for term in terms
    }
    for term in query_terms(" ".join(identifiers)):
        weights[term] = weights.get(term, 1.0) * 3
    return weights


def identifier_rank(hit: Evidence, identifiers: list[str]) -> int:
    if not identifiers:
        return 0
    names = fold(" ".join([
        hit.title, hit.source_path, hit.document_code or "", hit.source_uri, *hit.original_sources,
    ]))
    content = fold(hit.content)
    keys = [fold(value) for value in identifiers]
    if any(key in names for key in keys):
        return -2
    return -1 if any(key in content for key in keys) else 0


def prioritize(hits: list[Evidence], identifiers: list[str]) -> list[Evidence]:
    """Stable identifier promotion; otherwise keep the focused Search order."""
    return [
        hit for _, _, hit in sorted(
            (identifier_rank(hit, identifiers), index, hit) for index, hit in enumerate(hits)
        )
    ]


def best_window(content: str, weights: dict[str, float], size: int) -> tuple[int, int]:
    if len(content) <= size:
        return 0, len(content)
    matches = [
        (match.start(), match.end(), term) for match in _WORD.finditer(content)
        if (term := fold(match.group())) in weights
    ]
    best: tuple[float, int, int] = (0.0, 0, 0)
    left = 0
    for right in range(len(matches)):
        while left < right and matches[right][1] - matches[left][0] > size:
            left += 1
        score = sum(weights[term] for term in {m[2] for m in matches[left:right + 1]})
        if score > best[0]:
            best = (score, matches[left][0], matches[right][1])
    middle = (best[1] + best[2]) // 2 if best[0] else 0
    start = max(0, min(len(content) - size, middle - size // 2))
    end = start + size
    if start > 0 and (space := content.find(" ", start, start + 80)) >= 0:
        start = space + 1
    if end < len(content) and (space := content.rfind(" ", end - 80, end)) > start:
        end = space
    return start, end


def locate(content: str, caption: str) -> tuple[int, int] | None:
    """Span of a semantic caption in content, tolerating whitespace differences only."""
    caption = caption.strip()
    if not caption:
        return None
    if (start := content.find(caption)) >= 0:
        return start, start + len(caption)
    words = caption.split()[:40]
    if len(words) < 3:
        return None
    match = re.search(r"\s+".join(re.escape(word) for word in words), content)
    return (match.start(), match.end()) if match else None


def centered(content: str, span: tuple[int, int], size: int) -> tuple[int, int]:
    if len(content) <= size:
        return 0, len(content)
    middle = (span[0] + span[1]) // 2
    start = max(0, min(len(content) - size, middle - size // 2))
    end = start + size
    if start > 0 and (space := content.find(" ", start, start + 80)) >= 0 and space < span[0]:
        start = space + 1
    if end < len(content) and (space := content.rfind(" ", end - 80, end)) > span[1]:
        end = space
    return start, end


def excerpt(
    hit: Evidence, weights: dict[str, float], size: int, anchor: Literal["lexical", "caption"],
) -> tuple[int, int]:
    if anchor == "caption" and hit.caption and (span := locate(hit.content, hit.caption)):
        return centered(hit.content, span, size)
    return best_window(hit.content, weights, size)


def apply_ranking(hits: list[Evidence], ranked_ids: list[str]) -> list[Evidence]:
    """Ranked IDs first (unknown and duplicate IDs ignored), then the rest in prior order."""
    by_id = {hit.id: hit for hit in hits}
    first = [by_id[key] for key in dict.fromkeys(ranked_ids) if key in by_id]
    chosen = {hit.id for hit in first}
    return first + [hit for hit in hits if hit.id not in chosen]


def build_pack(
    hits: list[Evidence], query: str, identifiers: list[str], policy: EvidencePolicy,
) -> Pack:
    ordered = prioritize(hits, identifiers)
    weights = term_weights(query, ordered, identifiers)
    return Pack(
        shown=[
            (hit, excerpt(hit, weights, policy.excerpt_characters, policy.anchor))
            for hit in ordered[:policy.pack_size]
        ],
        previews=[
            (hit, hit.content[slice(*best_window(hit.content, weights, PREVIEW_CHARACTERS))])
            for hit in ordered[policy.pack_size:]
        ],
        weights=weights,
    )


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def exposed_spans(evidence: Evidence) -> list[tuple[int, int]]:
    return evidence.exposed if evidence.exposed is not None else [(0, len(evidence.content))]


def exposed_text(evidence: Evidence) -> list[str]:
    return [evidence.content[start:end] for start, end in exposed_spans(evidence)]


def expose(evidence: Evidence, span: tuple[int, int]) -> Evidence:
    return evidence.model_copy(update={"exposed": merge_spans([span])})


def merge_exposure(left: Evidence, right: Evidence) -> Evidence:
    if left.id != right.id or left.content != right.content:
        raise ValueError("Exposure can only merge identical evidence")
    if left.exposed is None or right.exposed is None:
        return left.model_copy(update={"exposed": None})
    return left.model_copy(update={"exposed": merge_spans([*left.exposed, *right.exposed])})
