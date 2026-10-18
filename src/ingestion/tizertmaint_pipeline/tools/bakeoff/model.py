"""Bake-off records: sample documents, gold items, per-page arm evidence and scores."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAMPLE_SCHEMA = "tizert-bakeoff-sample.v1"
GOLD_SCHEMA = "tizert-bakeoff-gold.v1"
REPORT_SCHEMA = "tizert-bakeoff-report.v1"

Split = Literal["tuning", "held_out"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SampleDocument(_Frozen):
    id: str
    blob_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    split: Split | Literal["mixed"]
    tuning_pages: tuple[int, ...] = ()
    """For ``mixed`` documents: pages profiles may be tuned on; every other page is held out."""
    upload_from: str | None = None
    """Repository path of a document that is not in the corpus and must be uploaded first."""
    observed_type: str | None = None

    def split_of(self, page: int) -> Split:
        if self.split == "mixed":
            return "tuning" if page in self.tuning_pages else "held_out"
        return self.split


class Exclusion(_Frozen):
    id: str
    reason: str


class Sample(_Frozen):
    schema_name: str = Field(default=SAMPLE_SCHEMA, alias="schema")
    arms: tuple[str, ...]
    cu_arms: tuple[str, ...]
    caps: dict[str, int]
    documents: tuple[SampleDocument, ...]
    excluded: tuple[Exclusion, ...] = ()
    estimate: dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    def document(self, doc_id: str) -> SampleDocument:
        for document in self.documents:
            if document.id == doc_id:
                return document
        raise KeyError(doc_id)


Check = Literal["literal", "absent", "field"]


class GoldItem(_Frozen):
    """One verifiable observation on one original page; drafted, then SME spot-checked."""

    id: str
    doc: str
    page: int = Field(ge=1)
    check: Check
    value: str
    alternatives: tuple[str, ...] = ()
    """Equivalent printed forms, e.g. a decimal comma and a decimal point."""
    field: str | None = None
    """Top-level analyzer field for ``field`` checks."""
    category: str
    seed: str
    """Where the expectation comes from (review section, discrepancy ID)."""
    note: str | None = None
    status: Literal["draft", "sme_checked", "rejected"] = "draft"

    @model_validator(mode="after")
    def _field_checks_name_a_field(self) -> GoldItem:
        if (self.check == "field") != (self.field is not None):
            raise ValueError(f"{self.id}: only field checks name a field")
        return self

    @property
    def forms(self) -> tuple[str, ...]:
        return (self.value, *self.alternatives)


class FieldValue(_Frozen):
    field: str
    path: str
    method: str
    value: Any = None
    raw_text: str | None = None
    confidence: float | None = None


class PageEvidence(_Frozen):
    source_text: str = ""
    generated_text: str = ""
    fields: tuple[FieldValue, ...] = ()
    routes: tuple[str, ...] = ()
    """Extraction methods that produced this page's evidence."""


class DocumentEvidence(_Frozen):
    doc: str
    found: bool
    complete: bool = False
    evidence_id: str | None = None
    plan_id: str | None = None
    pages: dict[int, PageEvidence] = Field(default_factory=dict)


class Usage(_Frozen):
    provider: str
    pages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0


class ItemResult(_Frozen):
    id: str
    doc: str
    page: int
    check: Check
    category: str
    split: Split
    outcome: Literal["pass", "fail", "missing_evidence", "not_applicable"]
    where: tuple[str, ...] = ()
    """Evidence kinds where the value was found (source, generated, field)."""


class ArmScore(_Frozen):
    arm: str
    documents: int
    documents_found: int
    documents_complete: int
    usage: tuple[Usage, ...] = ()
    jobs: dict[str, int] = Field(default_factory=dict)
    """CU submits, reused results, escalations and unfinished jobs of the arm's plans."""
    metrics: dict[str, dict[str, float | int | None]] = Field(default_factory=dict)
    items: tuple[ItemResult, ...] = ()


class Report(_Frozen):
    schema_name: str = Field(default=REPORT_SCHEMA, alias="schema")
    generated_at: str
    sample_sha256: str
    gold_sha256: str
    gold_items: int
    arms: tuple[ArmScore, ...]

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
