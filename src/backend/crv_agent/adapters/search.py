from typing import Literal

from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizableTextQuery
from pydantic import Field

from crv_agent.domain.evidence import Evidence
from crv_agent.domain.planning import Contract

SELECT = [
    "projection_id", "release_id", "chunk_id", "content_hash", "title", "content",
    "source_uri", "source_path", "document_code", "original_page_numbers", "source_locators",
    "original_source_uris",
    "is_generated", "document_class",
    "citation_basis", "extraction_route", "original_content_hashes",
]


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def query_filter(release: str, hashes: list[str]) -> str:
    result = f"release_id eq {literal(release)}"
    if hashes:
        if len(hashes) > 1000 or any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in hashes
        ):
            raise ValueError("Invalid content hash filter")
        result += f" and search.in(content_hash, {literal(','.join(hashes))}, ',')"
    return result


class SearchRow(Contract):
    projection_id: str
    release_id: str
    chunk_id: str
    content_hash: str
    title: str
    content: str
    source_uri: str
    source_path: str | None = None
    document_code: str | None = None
    original_page_numbers: list[int]
    source_locators: list[str] = Field(default_factory=list)
    original_source_uris: list[str] = Field(default_factory=list)
    is_generated: bool
    document_class: str | None
    citation_basis: str | None
    extraction_route: str | None
    original_content_hashes: list[str]


class AzureSearch:
    def __init__(self, client: SearchClient, release: str) -> None:
        self.client = client
        self.release = release

    async def ready(self) -> None:
        rows = await self.client.search(
            search_text="*", filter=query_filter(self.release, []), top=1, select=SELECT,
        )
        if not [row async for row in rows]:
            raise ValueError("Configured Search release contains no evidence")

    async def query(
        self, text: str, release: str, mode: Literal["lexical", "semantic"],
        hashes: list[str], top: int = 50, captions: bool = False,
    ) -> list[Evidence]:
        semantic = mode == "semantic"
        rows = await self.client.search(
            search_text=text, filter=query_filter(release, hashes), top=top, select=SELECT,
            query_type="semantic" if semantic else "simple",
            semantic_configuration_name="crv-evidence-semantic" if semantic else None,
            query_caption="extractive" if semantic and captions else None,
            query_caption_highlight_enabled=False if semantic and captions else None,
            vector_queries=[
                VectorizableTextQuery(text=text, k_nearest_neighbors=50, fields="content_vector")
            ] if semantic else None,
        )
        result = []
        async for raw in rows:
            row = SearchRow.model_validate({key: raw.get(key) for key in SELECT})
            found = raw.get("@search.captions") or []
            caption = next((
                value for item in found
                if (value := item.get("text") if isinstance(item, dict)
                    else getattr(item, "text", None))
            ), None)
            result.append(Evidence(
                id=row.projection_id, release=row.release_id, chunk_id=row.chunk_id,
                content_hash=row.content_hash, title=row.title, content=row.content,
                source_uri=row.source_uri, source_path=row.source_path or "",
                document_code=row.document_code, pages=row.original_page_numbers,
                locators=row.source_locators, original_sources=row.original_source_uris,
                generated=row.is_generated, document_class=row.document_class or "unknown",
                citation_basis=row.citation_basis or "unknown",
                extraction_route=row.extraction_route or "unknown",
                original_content_hashes=row.original_content_hashes, caption=caption,
            ))
        return result
