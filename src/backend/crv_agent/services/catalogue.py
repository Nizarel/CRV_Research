from crv_agent.domain.answers import Coverage
from crv_agent.domain.execution import Budget
from crv_agent.domain.planning import Document
from crv_agent.ports.knowledge import Knowledge


async def enumerate_documents(
    knowledge: Knowledge, entities: list[str], release: str, budget: Budget,
) -> tuple[list[Document], Coverage]:
    documents: dict[str, Document] = {}
    cursor = None
    total = 0
    while True:
        await budget.reserve(retrieval=1)
        page = await budget.wait(knowledge.documents(entities, release, cursor))
        total = page.total
        for document in page.documents:
            documents[document.iri] = document
        if page.next_cursor is None:
            break
        if page.next_cursor == cursor:
            raise ValueError("Catalogue cursor did not advance")
        cursor = page.next_cursor
    return list(documents.values()), Coverage(
        matched=total, enumerated=len(documents), catalogue_complete=True,
        gaps=["KG enumeration excludes collections not represented in the pinned graph."],
    )
