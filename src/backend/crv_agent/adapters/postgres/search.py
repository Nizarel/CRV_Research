"""Evidence search in PostgreSQL (``crv_retrieval``, ingestion migration 0022) behind the Search
port. The database fuses keyword, identifier and vector legs; an optional cross-encoder reorders
semantic results, as the Azure semantic ranker does. Rows are the Azure Search rows."""

import json
from typing import Literal, Protocol

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.adapters.search import SELECT, SearchRow, check_hashes, check_table_scope
from crv_agent.domain.evidence import Evidence

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
RERANK_DEPTH = 50
PASSAGE_CHARACTERS = 4000


class QueryEmbedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class CrossEncoder(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        """Candidate indexes, most relevant first."""
        ...


def passage(hit: Evidence) -> str:
    """Rerank input in the semantic configuration's order: title, keywords, then content."""
    head = "\n".join(value for value in (hit.title, hit.document_code, hit.source_path) if value)
    return f"{head}\n{hit.content[:PASSAGE_CHARACTERS]}"


def _evidence(row: DictRow) -> Evidence:
    return SearchRow.model_validate({key: row[key] for key in SELECT}).evidence()


class PostgresSearch:
    def __init__(
        self, pool: Pool, release: str, embedder: QueryEmbedder, *,
        model: Literal["te3"] = "te3", reranker: CrossEncoder | None = None,
    ) -> None:
        self.pool = pool
        self.release = release
        self.embedder = embedder
        self.model = model
        self.reranker = reranker

    async def ready(self) -> None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM crv_retrieval.release_status(%s)", (self.release,),
            )
            row = await cursor.fetchone()
        if row is None or not row["chunks"]:
            raise ValueError("Configured PostgreSQL release contains no evidence")
        if row[self.model] != row["chunks"]:
            raise ValueError(
                f"{row['chunks'] - row[self.model]} chunk(s) lack {self.model} vectors",
            )

    async def query(
        self, text: str, release: str, mode: Literal["lexical", "semantic"],
        hashes: list[str], top: int = 50, captions: bool = False,
    ) -> list[Evidence]:
        check_hashes(hashes)
        reranker = self.reranker if mode == "semantic" else None
        vector = await self.embedder.embed(text) if mode == "semantic" else None
        depth = max(top, RERANK_DEPTH) if reranker else top
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM crv_retrieval.search(%s, %s, %s::text[], %s, %s::real[], %s)",
                (release, text, hashes, self.model if vector else None, vector, depth),
            )
            hits = [_evidence(row) for row in await cursor.fetchall()]
        if reranker is not None and len(hits) > 1:
            order = await reranker.rerank(text, [passage(hit) for hit in hits])
            ranked = set(order)
            hits = [hits[i] for i in order] + [h for i, h in enumerate(hits) if i not in ranked]
        return hits[:top]

    async def tables(self, release: str, pages: dict[str, list[int]]) -> list[Evidence]:
        if not pages:
            return []
        check_table_scope(pages)
        scopes = {digest: sorted(set(numbers)) for digest, numbers in pages.items()}
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM crv_retrieval.table_neighbors(%s, %s::jsonb)",
                (release, json.dumps(scopes)),
            )
            return [_evidence(row) for row in await cursor.fetchall()]
