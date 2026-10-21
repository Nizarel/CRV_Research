"""Assemble (plan barrier): build the evidence manifest over all original pages, then chunk.

Runs once every unit of the plan has a disposition. Missing pages keep the reason of the
unit that failed; a partial document needs review unless partial projection is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.chunks import CHUNK_SET_SCHEMA
from ..domain.errors import IntegrityError
from ..domain.evidence import (
    DocumentPage,
    EvidenceDocument,
    EvidenceUnit,
    PageStatus,
    UnitSummary,
)
from ..domain.furniture import FurnitureScan
from ..domain.ids import Sha256Hex, fingerprint
from ..domain.models import ArtifactLink, StageJob, StageOutcome, UnitDisposition, UnitState
from ..domain.observations import ScientificObservation, numeric_observations
from ..domain.routing import Route
from ..domain.states import JobStatus, StageName
from ..ports.evidence import ChunkRecord, EvidenceRecord, EvidenceStatus
from .base import StageContext
from .chunkers import CHUNKER, ChunkingSettings, DocumentChunker
from .common import load_model, parse_inputs, put_json, put_jsonl
from .links import _Inputs

# 2: evidence-chunker@5 splits oversized figure/generated text (embed quarantined 8191+ tokens).
STAGE_VERSION = "4"
_GAPS = frozenset(
    {UnitDisposition.FAILED, UnitDisposition.UNSUPPORTED, UnitDisposition.QUARANTINED}
)
_WITHDRAWN = frozenset({UnitDisposition.SUPERSEDED, UnitDisposition.CANCELLED})


class AssembleInputs(_Inputs):
    plan_id: str
    content_hash: Sha256Hex
    policy_version: str
    family: str
    page_count: int | None = None


class EmbedInputs(_Inputs):
    evidence_id: str
    content_hash: Sha256Hex
    policy_version: str


def _summary(state: UnitState) -> UnitSummary:
    unit = state.unit
    return UnitSummary(
        unit_id=unit.unit_id,
        ordinal=unit.ordinal,
        route=unit.route,
        kind=unit.kind,
        pages=unit.pages,
        disposition=(state.disposition or UnitDisposition.FAILED).value,
        reason=unit.reason,
        evidence=state.evidence,
    )


def _unit_pages(state: UnitState, evidence: EvidenceUnit | None) -> list[DocumentPage]:
    unit = state.unit
    disposition = state.disposition
    if evidence is not None:
        statuses = {page.page: page for page in evidence.pages}
        return [
            DocumentPage(
                page=page,
                status=statuses[page].status if page in statuses else PageStatus.CONTENT,
                unit_id=unit.unit_id,
                reason=statuses[page].reason if page in statuses else None,
            )
            for page in unit.pages
        ]
    if disposition is UnitDisposition.NO_EVIDENCE:
        status = PageStatus.BLANK if unit.route == Route.BLANK.value else PageStatus.NO_TEXT
    else:
        status = PageStatus.MISSING
    return [
        DocumentPage(page=page, status=status, unit_id=unit.unit_id, reason=unit.reason)
        for page in unit.pages
    ]


@dataclass(frozen=True)
class ChunkStage:
    settings: ChunkingSettings = field(default_factory=ChunkingSettings)

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(AssembleInputs, job)
        services = context.services
        tokens = services.require_tokens()
        states = await services.require_plans().plan_units(inputs.plan_id)
        if not states:
            raise IntegrityError(f"Plan {inputs.plan_id} has no units")
        open_units = [state.unit.unit_id for state in states if state.disposition is None]
        if open_units:
            raise IntegrityError(f"Barrier released with {len(open_units)} open unit(s)")
        withdrawn = [s for s in states if s.disposition in _WITHDRAWN]
        if withdrawn:
            return StageOutcome.superseded(
                f"{len(withdrawn)} unit(s) withdrawn; a newer plan owns this content"
            )
        gaps = [state for state in states if state.disposition in _GAPS]
        completeness = "partial" if gaps else "complete"
        evidence_id = "evd:" + fingerprint(
            "evidence",
            {
                "plan_id": inputs.plan_id,
                "units": [
                    [s.unit.unit_id, s.disposition, s.evidence.sha256 if s.evidence else None]
                    for s in states
                ],
                "chunker": CHUNKER,
                "tokenizer": tokens.name,
                "settings": self.settings.identity(),
            },
        )
        chunker = DocumentChunker(evidence_id=evidence_id, tokens=tokens, settings=self.settings)
        # Pass 1: repeated running lines and header codes need the whole document.
        scan = FurnitureScan()
        for state in states:
            if state.evidence is not None:
                scan.add(await load_model(services, state.evidence, EvidenceUnit))
        chunker.running = scan.running()
        pages: list[DocumentPage] = []
        observations: list[ScientificObservation] = []
        for state in states:
            unit_evidence = None
            if state.evidence is not None:
                unit_evidence = await load_model(services, state.evidence, EvidenceUnit)
                chunker.feed(unit_evidence)
                if self.settings.scientific_observations:
                    observations.extend(numeric_observations(unit_evidence))
            pages.extend(_unit_pages(state, unit_evidence))
        covered = {page.page for page in pages}
        for page in range(1, (inputs.page_count or 0) + 1):
            if page not in covered:
                pages.append(
                    DocumentPage(page=page, status=PageStatus.MISSING, reason="page not planned")
                )
                completeness = "partial"
        manifest = EvidenceDocument(
            evidence_id=evidence_id,
            plan_id=inputs.plan_id,
            content_hash=inputs.content_hash,
            policy_version=inputs.policy_version,
            family=inputs.family,
            page_count=inputs.page_count,
            units=tuple(_summary(state) for state in states),
            pages=tuple(sorted(pages, key=lambda page: page.page)),
            completeness=completeness,
            document_codes=scan.document_codes(),
        )
        manifest_ref = await put_json(services, manifest, schema_name=manifest.schema_name)
        links = [ArtifactLink(role="evidence_manifest", artifact=manifest_ref)]
        if self.settings.scientific_observations:
            observation_ref = await put_jsonl(
                services, observations, schema_name="scientific-observations.v1"
            )
            links.append(ArtifactLink(role="scientific_observations", artifact=observation_ref))
        checkpoint = {"evidence_id": evidence_id, "completeness": completeness, "gaps": len(gaps)}
        if completeness != "complete" and not self.settings.allow_partial_projection:
            return StageOutcome(
                status=JobStatus.NEEDS_REVIEW,
                reason=f"partial evidence: {len(gaps)} unit(s) failed or unsupported",
                artifacts=tuple(links),
                checkpoint=checkpoint,
            )
        chunks = chunker.finish()
        if not chunks:
            return StageOutcome(
                status=JobStatus.NO_EVIDENCE,
                reason="no chunkable evidence in any unit",
                artifacts=tuple(links),
                checkpoint=checkpoint,
            )
        chunk_set = await put_jsonl(services, chunks, schema_name=CHUNK_SET_SCHEMA)
        links.append(ArtifactLink(role="chunk_set", artifact=chunk_set))
        now = services.clock.now()
        await services.require_evidence().record_document(
            EvidenceRecord(
                evidence_id=evidence_id,
                plan_id=inputs.plan_id,
                content_hash=inputs.content_hash,
                policy_version=inputs.policy_version,
                completeness=completeness,
                status=EvidenceStatus.CHUNKED,
                manifest=manifest_ref,
                chunk_set=chunk_set,
                chunk_count=len(chunks),
                created_at=now,
                updated_at=now,
            ),
            [
                ChunkRecord(
                    chunk_id=chunk.chunk_id,
                    evidence_id=evidence_id,
                    ordinal=chunk.ordinal,
                    kind=chunk.kind.value,
                    chunker=chunk.chunker,
                    tokenizer=chunk.tokenizer,
                    text_sha256=chunk.text_sha256,
                    token_count=chunk.token_count,
                    pages=chunk.pages,
                )
                for chunk in chunks
            ],
        )
        embed = EmbedInputs(
            evidence_id=evidence_id,
            content_hash=inputs.content_hash,
            policy_version=inputs.policy_version,
        )
        return StageOutcome.succeeded(
            artifacts=tuple(links),
            next_jobs=(
                services.planner.spec(
                    StageName.EMBED,
                    unit_id=evidence_id,
                    inputs=embed.model_dump(mode="json"),
                    content_hash=inputs.content_hash,
                ),
            ),
            checkpoint={**checkpoint, "chunks": len(chunks)},
        )
