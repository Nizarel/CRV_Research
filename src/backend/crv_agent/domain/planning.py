from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Facet(Contract):
    question: str = Field(min_length=1, max_length=4000)


class QueryPlan(Contract):
    intent: Literal[
        "lookup", "synthesis", "timeline", "comparison", "catalogue",
        "calculation", "clarify", "out_of_scope", "procedure",
    ]
    language: Literal["fr", "en"]
    facets: list[Facet] = Field(max_length=6)
    identifiers: list[str]
    premises: list[str]
    clarification: str | None


class Entity(Contract):
    iri: str
    name: str
    kind: str
    status: str


class Document(Contract):
    iri: str
    title: str
    content_hash: str | None
    date: str | None
    status: str
    source_paths: list[str]


class DocumentPage(Contract):
    documents: list[Document]
    next_cursor: str | None
    total: int


class StudyRef(Contract):
    """A KG-2 study (candidate unless reviewed): steers retrieval, never cited directly."""

    iri: str
    label: str
    scale: str | None
    period: str | None
    facility: str | None
    status: str
    content_hash: str | None
    source_paths: list[str]
    pages: list[int]


class ObservationRef(Contract):
    """A KG-2 observation with its page and quote; the agent re-reads the source to cite it."""

    property: str
    element: str | None
    stream: str | None
    value_text: str
    unit: str | None
    status: str
    subject: str | None
    study: str | None
    period: str | None
    content_hash: str | None
    source_paths: list[str]
    page: int | None
    quote: str
