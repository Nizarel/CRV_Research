from typing import Literal, Protocol

from crv_agent.domain.evidence import Evidence


class Search(Protocol):
    async def query(
        self, text: str, release: str, mode: Literal["lexical", "semantic"],
        hashes: list[str], top: int = 50,
    ) -> list[Evidence]: ...

    async def ready(self) -> None: ...
