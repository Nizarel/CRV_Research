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

MODES = ("bm25", "hybrid", "semantic", "kg")
SELECT = ["chunk_id", "content_hash", "title", "source_path", "content",
          "original_page_numbers", "document_code"]
RRF_K = 60
MAX_KG_DOCUMENTS = 150


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
        if mode != "kg":
            hits = await self._query(text, mode, top)
            return Retrieved(hits, time.perf_counter() - started)
        if self._kg is None:
            raise ValueError("The kg mode needs the crv_kg read API")
        entities = await self._kg.resolve(text)
        iris = [entity["iri"] for entity in entities]
        hashes = await self._kg.documents(iris) if iris else []
        base = await self._query(text, "semantic", top)
        if not hashes:
            return Retrieved(base, time.perf_counter() - started, entities, 0)
        joined = ",".join(hashes[:MAX_KG_DOCUMENTS])
        steered = await self._query(
            text, "semantic", top, f"search.in(content_hash, '{joined}', ',')"
        )
        return Retrieved(_fuse([steered, base], top), time.perf_counter() - started,
                         entities, len(hashes))


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
