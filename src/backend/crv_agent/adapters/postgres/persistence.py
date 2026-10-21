from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from crv_agent.ports.persistence import (
    Run,
    RunStatus,
    Thread,
    ThreadNotFound,
    TurnConflict,
)


class PostgresPersistence:
    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[DictRow]]) -> None:
        self.pool = pool

    async def create_thread(self, owner: str, search_release: str, kg_release: str) -> Thread:
        thread = Thread(
            id=str(uuid4()), owner=owner, search_release=search_release, kg_release=kg_release,
        )
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO crv_threads(id, owner, search_release, kg_release)"
                " VALUES (%s, %s, %s, %s)",
                (thread.id, owner, search_release, kg_release),
            )
        return thread

    async def threads(self, owner: str) -> list[Thread]:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT id, owner, search_release, kg_release FROM crv_threads"
                " WHERE owner = %s ORDER BY created_at DESC", (owner,),
            )
            return [Thread.model_validate(row) for row in await result.fetchall()]

    async def thread(self, owner: str, thread_id: str) -> Thread:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT id, owner, search_release, kg_release FROM crv_threads"
                " WHERE id = %s AND owner = %s", (thread_id, owner),
            )
            row = await result.fetchone()
            if row is None:
                raise ThreadNotFound(thread_id)
            return Thread.model_validate(row)

    async def history(self, owner: str, thread_id: str) -> list[Run]:
        await self.thread(owner, thread_id)
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "SELECT id, thread_id, question, status, result FROM crv_runs"
                " WHERE thread_id = %s ORDER BY created_at, id", (thread_id,),
            )
            return [Run.model_validate(row) for row in await result.fetchall()]

    async def start_run(
        self, owner: str, thread_id: str, request_id: str, question: str,
    ) -> tuple[Run, bool]:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '3s'")
            locked = await conn.execute(
                "SELECT id FROM crv_threads WHERE id = %s AND owner = %s FOR UPDATE",
                (thread_id, owner),
            )
            if await locked.fetchone() is None:
                raise ThreadNotFound(thread_id)
            # A killed pod cannot retain a running turn forever. The configured turn deadline
            # is shorter than this recovery window; a stale run is never restarted implicitly.
            await conn.execute(
                "UPDATE crv_runs SET status = 'failed',"
                " result = '{\"error\":\"worker_lost\"}'::jsonb, updated_at = now()"
                " WHERE thread_id = %s AND status = 'running'"
                " AND updated_at < now() - interval '5 minutes'", (thread_id,),
            )
            previous = await conn.execute(
                "SELECT id, thread_id, question, status, result FROM crv_runs"
                " WHERE thread_id = %s AND id = %s", (thread_id, request_id),
            )
            if row := await previous.fetchone():
                if row["question"] != question:
                    raise TurnConflict("Request ID already used with a different message")
                return Run.model_validate(row), False
            active = await conn.execute(
                "SELECT 1 FROM crv_runs WHERE thread_id = %s AND status = 'running'",
                (thread_id,),
            )
            if await active.fetchone():
                raise TurnConflict("Another turn is running in this conversation")
            await conn.execute(
                "INSERT INTO crv_runs(thread_id, id, question, status) VALUES (%s,%s,%s,'running')",
                (thread_id, request_id, question),
            )
        return Run(
            id=request_id, thread_id=thread_id, question=question, status="running", result=None,
        ), True

    async def finish_run(
        self, owner: str, thread_id: str, request_id: str, status: RunStatus,
        result: dict[str, object],
    ) -> None:
        if status == "running":
            raise ValueError("A final status is required")
        await self.thread(owner, thread_id)
        async with self.pool.connection() as conn:
            updated = await conn.execute(
                "UPDATE crv_runs SET status=%s, result=%s, updated_at=now()"
                " WHERE thread_id=%s AND id=%s AND status='running' RETURNING id",
                (status, Jsonb(result), thread_id, request_id),
            )
            if await updated.fetchone() is None:
                raise TurnConflict("Turn is not active")

    async def feedback(
        self, owner: str, thread_id: str, request_id: str, rating: int, comment: str,
    ) -> None:
        await self.thread(owner, thread_id)
        if rating not in {-1, 1}:
            raise ValueError("Rating must be -1 or 1")
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "INSERT INTO crv_feedback(thread_id, run_id, rating, comment)"
                " SELECT thread_id, id, %s, %s FROM crv_runs"
                " WHERE thread_id=%s AND id=%s AND status='completed'"
                " ON CONFLICT (thread_id,run_id) DO UPDATE"
                " SET rating=EXCLUDED.rating, comment=EXCLUDED.comment RETURNING run_id",
                (rating, comment, thread_id, request_id),
            )
            if await result.fetchone() is None:
                raise ThreadNotFound(request_id)
