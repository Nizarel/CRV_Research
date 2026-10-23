from typing import Protocol

from crv_agent.domain.planning import DocumentPage, Entity, ObservationRef, StudyRef


class Knowledge(Protocol):
    async def resolve(self, text: str, release: str) -> list[Entity]: ...

    async def documents(
        self, entities: list[str], release: str, cursor: str | None,
    ) -> DocumentPage: ...

    async def lineage(self, iri: str, release: str) -> list[dict[str, object]]: ...

    async def studies(self, entities: list[str], release: str) -> list[StudyRef]: ...

    async def observations(
        self, entities: list[str], properties: list[str] | None, release: str,
    ) -> list[ObservationRef]: ...

    async def ready(self, release: str) -> None: ...
