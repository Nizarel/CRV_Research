from typing import Literal, Protocol

from crv_agent.domain.planning import Contract

RunStatus = Literal["running", "completed", "failed", "cancelled"]


class ThreadNotFound(LookupError):
    pass


class TurnConflict(RuntimeError):
    pass


class Thread(Contract):
    id: str
    owner: str
    search_release: str
    kg_release: str


class Run(Contract):
    id: str
    thread_id: str
    question: str
    status: RunStatus
    result: dict[str, object] | None


class Persistence(Protocol):
    async def create_thread(self, owner: str, search_release: str, kg_release: str) -> Thread: ...
    async def threads(self, owner: str) -> list[Thread]: ...
    async def thread(self, owner: str, thread_id: str) -> Thread: ...
    async def history(self, owner: str, thread_id: str) -> list[Run]: ...
    async def start_run(
        self, owner: str, thread_id: str, request_id: str, question: str,
    ) -> tuple[Run, bool]: ...
    async def finish_run(
        self, owner: str, thread_id: str, request_id: str, status: RunStatus,
        result: dict[str, object],
    ) -> None: ...
    async def feedback(
        self, owner: str, thread_id: str, request_id: str, rating: int, comment: str,
    ) -> None: ...
