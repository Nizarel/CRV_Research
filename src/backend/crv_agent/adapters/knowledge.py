import json

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from crv_agent.domain.planning import Document, DocumentPage, Entity


class PostgresKnowledge:
    def __init__(
        self, pool: AsyncConnectionPool[AsyncConnection[DictRow]], scopes: list[str],
    ) -> None:
        self.pool = pool
        self.scopes = scopes

    async def ready(self, release: str) -> None:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT * FROM crv_kg.list_documents(%s, %s, NULL, NULL, 1)",
                (release, self.scopes),
            )
            if await result.fetchone() is None:
                raise ValueError("Configured KG release has no visible documents")
            cursor = await conn.execute(
                "SELECT * FROM crv_kg.entity(%s, %s, %s)",
                (release, "urn:crv:readiness", self.scopes),
            )
            await cursor.fetchall()

    async def resolve(self, text: str, release: str) -> list[Entity]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT iri, name, kind, status FROM crv_kg.resolve(%s, %s, %s, 10)",
                (release, text, self.scopes),
            )
            return [Entity.model_validate(row) for row in await cursor.fetchall()]

    async def documents(
        self, entities: list[str], release: str, cursor: str | None,
    ) -> DocumentPage:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT * FROM crv_kg.list_documents(%s, %s, %s, %s, 201)",
                (release, self.scopes, entities or None, cursor),
            )
            rows = await result.fetchall()
        documents = []
        for row in rows[:200]:
            paths = row["source_paths"]
            if isinstance(paths, str):
                paths = json.loads(paths)
            documents.append(Document(
                iri=row["iri"], title=row["title"] or "",
                content_hash=row["content_hash"], date=row["date"], status=row["status"],
                source_paths=paths,
            ))
        return DocumentPage(
            documents=documents,
            next_cursor=documents[-1].iri if len(rows) > 200 else None,
            total=int(rows[0]["total_count"]) if rows else 0,
        )

    async def lineage(self, iri: str, release: str) -> list[dict[str, object]]:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT * FROM crv_kg.document_lineage(%s, %s, %s)",
                (release, iri, self.scopes),
            )
            return [dict(row) for row in await result.fetchall()]
