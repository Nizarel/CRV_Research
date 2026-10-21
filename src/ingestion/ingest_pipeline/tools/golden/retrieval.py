"""Retrieval modes over the evidence index, restricted to one Search release.

``bm25``: keywords only. ``hybrid``: keywords plus the index's integrated vectorizer.
``semantic``: hybrid reranked by the semantic ranker. ``kg``: the crv_kg read API resolves the
question's sites, organisations, projects and report codes, ``find_documents`` lists their
documents, and a semantic search restricted to those documents is fused (reciprocal rank)
with the unrestricted semantic search, so the graph steers retrieval without hiding text.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ...domain.crv_kg.vocab import fold, periodic_genre

MODES = ("bm25", "hybrid", "semantic", "kg", "semantic_focused", "kg_precise")
SELECT = ["chunk_id", "content_hash", "title", "source_path", "content",
          "original_page_numbers", "document_code"]
RRF_K = 60
MAX_KG_DOCUMENTS = 150
DEPTH = 50
"""Focused modes rerank this many semantic results."""
PERIODIC_PENALTY = 10
"""Rank positions a periodic summary (monthly activity report, executive summary...) loses."""
PER_DOCUMENT = 2
"""Chunks per document before further chunks of it lose ``REPEAT_PENALTY`` positions."""
REPEAT_PENALTY = 20
PRECISE_KINDS = frozenset({"Site", "ReportCode", "Organisation"})
"""Entity kinds precise enough to steer retrieval; cover-page projects are too broad."""


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    content_hash: str
    title: str
    source_path: str
    content: str
    pages: tuple[int, ...]
    document_code: str | None


@dataclass
class Retrieved:
    hits: list[Hit]
    seconds: float
    kg_entities: list[dict[str, Any]] = field(default_factory=list)
    kg_documents: int = 0


def _hit(row: Any) -> Hit:
    return Hit(
        chunk_id=str(row["chunk_id"]),
        content_hash=str(row.get("content_hash") or ""),
        title=str(row.get("title") or ""),
        source_path=str(row.get("source_path") or ""),
        content=str(row.get("content") or ""),
        pages=tuple(row.get("original_page_numbers") or ()),
        document_code=row.get("document_code"),
    )


class Retriever:
    def __init__(
        self,
        search: Any,
        *,
        release_id: str,
        semantic_configuration: str = "crv-evidence-semantic",
        kg: Any | None = None,
    ) -> None:
        self._search = search
        self._filter = f"release_id eq '{release_id}'"
        self._semantic = semantic_configuration
        self._kg = kg

    async def _query(
        self, text: str, mode: str, top: int, extra_filter: str | None = None
    ) -> list[Hit]:
        from azure.search.documents.models import VectorizableTextQuery

        query_filter = self._filter + (f" and {extra_filter}" if extra_filter else "")
        kwargs: dict[str, Any] = {
            "search_text": text, "filter": query_filter, "top": top, "select": SELECT,
        }
        if mode in ("hybrid", "semantic"):
            kwargs["vector_queries"] = [VectorizableTextQuery(
                text=text, k_nearest_neighbors=max(top, 50), fields="content_vector"
            )]
        if mode == "semantic":
            kwargs["query_type"] = "semantic"
            kwargs["semantic_configuration_name"] = self._semantic
        results = await self._search.search(**kwargs)
        return [_hit(row) async for row in results]

    async def retrieve(self, text: str, mode: str, top: int) -> Retrieved:
        started = time.perf_counter()
        if mode in ("bm25", "hybrid", "semantic"):
            hits = await self._query(text, mode, top)
            return Retrieved(hits, time.perf_counter() - started)
        if mode == "semantic_focused":
            hits = focus(await self._query(text, "semantic", max(top, DEPTH)), top)
            return Retrieved(hits, time.perf_counter() - started)
        if self._kg is None:
            raise ValueError("The kg modes need the crv_kg read API")
        entities = await self._kg.resolve(text)
        if mode == "kg_precise":
            entities = [entity for entity in entities if entity["kind"] in PRECISE_KINDS]
        iris = [entity["iri"] for entity in entities]
        hashes = await self._kg.documents(iris) if iris else []
        depth = max(top, DEPTH) if mode == "kg_precise" else top
        base = await self._query(text, "semantic", depth)
        if hashes:
            joined = ",".join(hashes[:MAX_KG_DOCUMENTS])
            steered = await self._query(
                text, "semantic", depth, f"search.in(content_hash, '{joined}', ',')"
            )
            hits = _fuse([steered, base], depth)
        else:
            hits = base
        if mode == "kg_precise":
            hits = focus(hits, top)
        return Retrieved(hits[:top], time.perf_counter() - started, entities, len(hashes))


def is_periodic(hit: Hit) -> bool:
    name = hit.source_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return periodic_genre(fold(name)) is not None


def focus(hits: Sequence[Hit], top: int) -> list[Hit]:
    """Soft rerank: periodic summaries and a document's extra chunks move down, never out."""

    seen: dict[str, int] = {}
    keyed: list[tuple[float, int, Hit]] = []
    for rank, hit in enumerate(hits, start=1):
        seen[hit.content_hash] = seen.get(hit.content_hash, 0) + 1
        key = rank + (PERIODIC_PENALTY if is_periodic(hit) else 0)
        if seen[hit.content_hash] > PER_DOCUMENT:
            key += REPEAT_PENALTY
        keyed.append((key, rank, hit))
    return [hit for _, _, hit in sorted(keyed, key=lambda item: (item[0], item[1]))][:top]


def _fuse(rankings: Sequence[list[Hit]], top: int) -> list[Hit]:
    scores: dict[str, float] = {}
    by_id: dict[str, Hit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            by_id.setdefault(hit.chunk_id, hit)
    ordered = sorted(scores, key=lambda chunk: (-scores[chunk], chunk))
    return [by_id[chunk] for chunk in ordered[:top]]


class KgReader:
    """The agent-facing crv_kg functions on the active release (execute-only role)."""

    def __init__(self, pool: Any, *, environment: str, scopes: Sequence[str]) -> None:
        self._pool = pool
        self._environment = environment
        self._scopes = list(scopes)
        self.release_id: str | None = None

    async def open(self) -> str:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT release_id FROM crv_kg.active_release($1)", self._environment
            )
        if row is None:
            raise ValueError(f"No active crv_kg release for {self._environment}")
        self.release_id = str(row["release_id"])
        return self.release_id

    async def resolve(self, text: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT iri, kind, name, status, method FROM crv_kg.resolve($1, $2, $3, 10)",
                self.release_id, text, self._scopes,
            )
        return [dict(row) for row in rows]

    async def documents(self, entities: Sequence[str]) -> list[str]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT content_hash FROM crv_kg.find_documents($1, $2, $3, p_limit => 200)",
                self.release_id, self._scopes, list(entities),
            )
        return [str(row["content_hash"]) for row in rows if row["content_hash"]]
