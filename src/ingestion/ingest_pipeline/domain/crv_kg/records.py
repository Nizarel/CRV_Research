"""KG-2 grounded extractions as candidate records (design §3.4 identifiers, §6 storage).

Studies and samples are scoped by their document (``urn:crv:study:<hash>/<n>``,
``urn:crv:sample:<study>/<slug>``); an observation's IRI hashes its locator, property and stream,
so re-running the same extraction yields the same records. Every record cites one text-span
evidence row (page + verbatim quote) and starts as ``candidate``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..crv import research_context
from ..ids import sha256_hex
from .build import document_iri
from .model import Assertion, Entity, Evidence, assertion_iri, evidence_iri
from .study import Grounding, Locator
from .table_rules import TableObservation
from .vocab import COLLECTION_SCOPES, EXCLUDED_COLLECTIONS, slug

EXTRACT_TOOL = "crv-kg-extract"
EXTRACT_VERSION = "1"


def document_scope(source_path: str) -> str | None:
    """Access scope of a document's records (None for collections the graph excludes)."""

    collection = research_context(source_path).collection
    return None if collection in EXCLUDED_COLLECTIONS else COLLECTION_SCOPES.get(collection)


@dataclass(frozen=True)
class ObservationRecord:
    iri: str
    document_iri: str
    study_iri: str | None
    sample_iri: str | None
    subject_text: str | None
    property: str
    element: str | None
    stream: str | None
    basis: str | None
    value_text: str
    value_num: Decimal | None
    unit: str | None
    qualifier: str | None
    method: str
    evidence_iri: str


@dataclass
class ExtractRecords:
    entities: list[Entity] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)


def _evidence(content_hash: str, source: str, scope: str, page: int, quote: str) -> Evidence:
    return Evidence(
        iri=evidence_iri(content_hash, "text_span", str(page), quote),
        source_hash=content_hash, source_name=source, locator_type="text_span",
        locator={"page": page, "quote": quote}, scope_key=scope,
    )


def observation_iri(content_hash: str, page: int, quote: str, prop: str, stream: str | None,
                    value: str) -> str:
    return "urn:crv:obs:" + sha256_hex(
        "|".join((content_hash, str(page), quote, prop, stream or "", value))
    )


def extraction_records(
    content_hash: str, source_path: str, scope: str, grounding: Grounding,
    tables: Sequence[tuple[int, TableObservation]] = (),
) -> ExtractRecords:
    """Candidate entities, assertions, evidence and observations of one document."""

    records = ExtractRecords()
    seen: set[str] = set()
    source = source_path.rsplit("/", 1)[-1]
    document = document_iri(content_hash)

    def locate(locator: Locator) -> str:
        item = _evidence(content_hash, source, scope, locator.page, locator.quote)
        if item.iri not in seen:
            seen.add(item.iri)
            records.evidence.append(item)
        return item.iri

    def link(subject: str, predicate: str, target: str, method: str, proof: str) -> None:
        records.assertions.append(Assertion(
            iri=assertion_iri(subject, predicate, target), subject=subject, predicate=predicate,
            object=target, method=method, status="candidate", evidence=(proof,),
        ))

    studies: list[str] = []
    samples: dict[tuple[int, str], str] = {}
    for n, study in enumerate(grounding.studies):
        iri = f"urn:crv:study:{content_hash}/{n}"
        studies.append(iri)
        proof = locate(study.locator)
        attributes: dict[str, Any] = study.model_dump(
            mode="json", exclude={"samples", "locator", "label"},
        )
        records.entities.append(Entity(
            iri=iri, kind="Study", label=study.label, status="candidate", attributes=attributes,
            identifiers=(), evidence=(proof,),
        ))
        link(document, "reports", iri, "llm", proof)
        for sample in study.samples:
            sample_iri = f"urn:crv:sample:{content_hash}/{n}/{slug(sample.name)}"
            sample_proof = locate(sample.locator)
            samples[(n, sample.name.casefold())] = sample_iri
            records.entities.append(Entity(
                iri=sample_iri, kind="Sample", label=sample.name, status="candidate",
                attributes=sample.model_dump(mode="json", exclude={"locator", "name"}),
                identifiers=(("name", sample.name),), evidence=(sample_proof,),
            ))
            link(iri, "uses", sample_iri, "llm", sample_proof)
    for grounded in grounding.observations:
        item = grounded.observation
        proof = locate(item.locator)
        study_iri = studies[item.study] if item.study is not None else None
        linked_sample = (
            samples.get((item.study, item.sample.casefold()))
            if item.study is not None and item.sample else None
        )
        records.observations.append(ObservationRecord(
            iri=observation_iri(content_hash, item.locator.page, item.locator.quote,
                                item.property, item.stream, item.value_text),
            document_iri=document, study_iri=study_iri, sample_iri=linked_sample,
            subject_text=None if linked_sample else (item.sample or item.site),
            property=item.property, element=item.element, stream=item.stream, basis=item.basis,
            value_text=item.value_text, value_num=grounded.value_num, unit=item.unit,
            qualifier=item.qualifier, method="llm", evidence_iri=proof,
        ))
    for page, table in tables:
        proof = locate(Locator(page=page, quote=table.quote))
        records.observations.append(ObservationRecord(
            iri=observation_iri(content_hash, page, table.quote, table.property, table.stream,
                                table.value_text),
            document_iri=document, study_iri=None, sample_iri=None, subject_text=None,
            property=table.property, element=table.element, stream=table.stream, basis=None,
            value_text=table.value_text, value_num=table.value, unit=table.unit,
            qualifier=table.qualifier, method="table", evidence_iri=proof,
        ))
    return records
