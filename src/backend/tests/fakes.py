import asyncio
from uuid import uuid4

from crv_agent.domain.answers import AnswerDraft, Verdict
from crv_agent.domain.evidence import CalculationRequest, Claim, Evidence, WorkerResult
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import (
    Document,
    DocumentPage,
    Entity,
    Facet,
    ObservationRef,
    QueryPlan,
    StudyRef,
)
from crv_agent.ports.persistence import Run, RunStatus, Thread, ThreadNotFound, TurnConflict

SOURCE = Evidence(
    id="S1", release="r1", chunk_id="c1", content_hash="a" * 64, title="Pilot report",
    content="Recovery 87.5 % at pilot scale.", source_uri="raw/report.pdf", pages=[4],
    locators=[], original_sources=[], generated=False, document_class="report",
)
CLAIM = Claim(
    text="Recovery was 87.5 %", evidence_ids=["S1"], quote=SOURCE.content, subject="sample A",
    quantity="recovery", value="87.5", unit="%", date=None, conditions="pilot", scope=None,
)


class FakeModels:
    def __init__(self, intent: str = "lookup", supported: bool = True) -> None:
        self.intent, self.supported = intent, supported
        self.compositions = 0

    async def plan(self, question: str, history: list[str], budget: Budget) -> QueryPlan:
        await budget.reserve(model=1, tokens=10)
        return QueryPlan.model_validate({
            "intent": self.intent, "language": "en",
            "facets": [Facet(question=question)], "identifiers": [], "premises": [],
            "clarification": "Which sample?",
        })

    async def compose(
        self, question: str, claims: list[Claim], context: str, budget: Budget,
    ) -> AnswerDraft:
        self.compositions += 1
        await budget.reserve(model=1, tokens=10)
        return AnswerDraft(
            content="Recovery was 87.5 % [S1]", claims=claims[:1], insufficient=False,
        )

    async def verify(
        self, draft: AnswerDraft, evidence: list[Evidence], context: str, budget: Budget,
    ) -> Verdict:
        await budget.reserve(model=1, tokens=10)
        return Verdict(
            supported=self.supported, reasons=[] if self.supported else ["Unsupported binding"],
        )

    async def calculation(
        self, question: str, claims: list[Claim], budget: Budget,
    ) -> CalculationRequest:
        return CalculationRequest(operation="unknown", input_claims=[], target_unit=None)


class FakeKnowledge:
    async def resolve(self, text: str, release: str) -> list[Entity]:
        return []

    async def documents(
        self, entities: list[str], release: str, cursor: str | None,
    ) -> DocumentPage:
        return DocumentPage(
            documents=[Document(
                iri="urn:doc:1", title=SOURCE.title, content_hash=SOURCE.content_hash,
                date=None, status="observed", source_paths=["report.pdf"],
            )], next_cursor=None, total=1,
        )

    async def lineage(self, iri: str, release: str) -> list[dict[str, object]]:
        return []

    async def studies(self, entities: list[str], release: str) -> list[StudyRef]:
        return []

    async def observations(
        self, entities: list[str], properties: list[str] | None, release: str,
    ) -> list[ObservationRef]:
        return []

    async def ready(self, release: str) -> None:
        return None


class FakeWorker:
    async def run(
        self, question: str, release: str, hashes: list[str], budget: Budget,
        *, strict: bool = False,
    ) -> tuple[WorkerResult, list[Evidence]]:
        await budget.reserve(retrieval=1)
        return WorkerResult(claims=[CLAIM], gaps=[]), [SOURCE]


class MemoryPersistence:
    """Explicit test adapter; never selected by runtime settings."""

    def __init__(self) -> None:
        self.records: dict[str, Thread] = {}
        self.runs: dict[tuple[str, str], Run] = {}
        self.lock = asyncio.Lock()

    async def create_thread(self, owner: str, search_release: str, kg_release: str) -> Thread:
        thread = Thread(
            id=str(uuid4()), owner=owner, search_release=search_release, kg_release=kg_release,
        )
        self.records[thread.id] = thread
        return thread

    async def threads(self, owner: str) -> list[Thread]:
        return [thread for thread in self.records.values() if thread.owner == owner]

    async def thread(self, owner: str, thread_id: str) -> Thread:
        if thread_id not in self.records or self.records[thread_id].owner != owner:
            raise ThreadNotFound(thread_id)
        return self.records[thread_id]

    async def history(self, owner: str, thread_id: str) -> list[Run]:
        await self.thread(owner, thread_id)
        return [run for run in self.runs.values() if run.thread_id == thread_id]

    async def start_run(
        self, owner: str, thread_id: str, request_id: str, question: str,
    ) -> tuple[Run, bool]:
        await self.thread(owner, thread_id)
        async with self.lock:
            if previous := self.runs.get((thread_id, request_id)):
                if previous.question != question:
                    raise TurnConflict("Duplicate request with different message")
                return previous, False
            if any(
                run.thread_id == thread_id and run.status == "running"
                for run in self.runs.values()
            ):
                raise TurnConflict("Running")
            run = Run(
                id=request_id, thread_id=thread_id, question=question,
                status="running", result=None,
            )
            self.runs[thread_id, request_id] = run
            return run, True

    async def finish_run(
        self, owner: str, thread_id: str, request_id: str, status: RunStatus,
        result: dict[str, object],
    ) -> None:
        await self.thread(owner, thread_id)
        self.runs[thread_id, request_id] = self.runs[thread_id, request_id].model_copy(
            update={"status": status, "result": result},
        )

    async def feedback(
        self, owner: str, thread_id: str, request_id: str, rating: int, comment: str,
    ) -> None:
        await self.thread(owner, thread_id)
        if (thread_id, request_id) not in self.runs:
            raise ThreadNotFound(request_id)
