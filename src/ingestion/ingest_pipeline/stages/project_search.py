"""Project embedded evidence into the shadow Search index: one row per chunk of a content.

Rows are keyed by (release, chunk) and shared by every placement of the same bytes: the
canonical placement (shortest path) names the row and the collection fields list them all.
Expected state lives in ``projection_item``; a row only counts once it is acknowledged and then
read back with the same payload digest. Rows of older evidence, or of a content no longer placed
anywhere, are retired only after the new rows verify. Access is deny-all until the ACL work of a
later phase.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from ..domain.chunks import EvidenceChunk, projection_key
from ..domain.errors import ErrorCategory, IntegrityError
from ..domain.evidence import EvidenceDocument, EvidenceKind, EvidenceUnit, FieldObservation
from ..domain.ids import sha256_hex
from ..domain.models import JobSpec, SourceSnapshot, StageJob, StageOutcome
from ..domain.projection import LIVE_STATES, ProjectionItem, ProjectionState
from ..domain.routing import Family, Route
from ..domain.states import JobStatus
from ..domain.vectors import Vector, encode_f32le
from ..ports.evidence import EvidenceRecord, EvidenceStatus
from .base import StageContext, StageServices
from .common import load_jsonl, load_model, parse_inputs
from .links import ProjectInputs, project_job
from .projection_fields import UnitMetadata, placement_metadata, row_metadata
from .research_fields import research_fields

STAGE_VERSION = "7"
KEY_FIELD = "projection_id"
DIGEST_FIELD = "payload_sha256"
PAGED_FAMILIES = frozenset({Family.PDF.value, Family.IMAGE.value})
FIELD_ROUTES = frozenset({Route.CU_ANALYZE.value, Route.NATIVE_DWG.value, Route.NATIVE_IFC.value})
"""Routes whose unit evidence carries extracted fields (codes, tags, revisions)."""


@dataclass(frozen=True)
class ProjectionSettings:
    target: str
    corpus: Literal["maintenance", "crv"] = "maintenance"
    store: str = "azure_search"
    release_id: str = "evidence-shadow-v1"
    access_scope_id: str = "unassigned"
    acl_policy_version: str = "phase2-deny-all"
    verify_deadline_seconds: float = 900.0
    retry_seconds: float = 20.0
    release_ids: Mapping[str, str] = field(default_factory=dict)
    """Release per routing policy version (bake-off arms); others use ``release_id``."""

    def for_policy(self, policy_version: str) -> ProjectionSettings:
        release = self.release_ids.get(policy_version)
        return self if release is None else replace(self, release_id=release)


def payload_digest(row: Mapping[str, Any]) -> str:
    """Digest of the row as projected; the vector contributes its own digest."""

    body = {key: value for key, value in row.items() if key != DIGEST_FIELD}
    vector = body.pop("content_vector", None)
    if vector is not None:
        body["content_vector_sha256"] = sha256_hex(encode_f32le(vector))
    return sha256_hex(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def canonical_order(placements: Iterable[SourceSnapshot]) -> list[SourceSnapshot]:
    """Shortest path first, then by name: the first placement names the row."""

    return sorted(placements, key=lambda s: (len(s.blob_name), s.blob_name, s.placement_id))


def build_row(
    chunk: EvidenceChunk,
    vector: Vector,
    *,
    placements: Sequence[SourceSnapshot],
    family: str,
    settings: ProjectionSettings,
    units: Mapping[str, UnitMetadata] | None = None,
    document_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """One row of ``infra/search/evidence-index-v2.json``; ``placements`` in canonical order."""

    canonical = placements[0]
    row: dict[str, Any] = {
        KEY_FIELD: projection_key(settings.release_id, chunk.chunk_id),
        "content": chunk.text,
        **row_metadata(
            chunk,
            placements=[placement_metadata(snapshot.blob_name) for snapshot in placements],
            units=units or {},
            family=family,
        ),
        "content_vector": list(vector),
        "release_id": settings.release_id,
        "chunk_id": chunk.chunk_id,
        "placement_id": canonical.placement_id,
        "placement_ids": [snapshot.placement_id for snapshot in placements],
        "source_snapshot_id": canonical.snapshot_id,
        "source_object_id": canonical.source_object_id,
        "content_hash": canonical.content_hash,
        "evidence_ids": list(chunk.block_ids),
        "language": chunk.language,
        "original_page_numbers": list(chunk.pages) if family in PAGED_FAMILIES else [],
        "source_locators": list(chunk.locators),
        "source_uri": f"{canonical.container_name}/{canonical.blob_name}",
        "evidence_kind": chunk.evidence_kind.value,
        "is_generated": chunk.evidence_kind is not EvidenceKind.SOURCE_TEXT,
        "access_scope_id": settings.access_scope_id,
        "acl_groups": [],
        "acl_users": [],
        "policy_version": settings.acl_policy_version,
    }
    row[DIGEST_FIELD] = payload_digest(row)
    if settings.corpus == "crv":
        row.update(research_fields(chunk, placements, document_codes=document_codes))
        for field_name in (
            "equipment_folder", "equipment_folders", "equipment_tags", "path_tags",
            "mentioned_tags", "zone_name", "zone_names", "process_case_id", "drawing_sheet_id",
        ):
            row.pop(field_name, None)
        row[DIGEST_FIELD] = payload_digest(row)
    return row


async def evidence_units(
    services: StageServices, record: EvidenceRecord
) -> tuple[str, dict[str, UnitMetadata], tuple[str, ...]]:
    """Manifest family, unit metadata and printed document codes.

    Only field-bearing units load their evidence.
    """

    manifest = await load_model(services, record.manifest, EvidenceDocument)
    units: dict[str, UnitMetadata] = {}
    for unit in manifest.units:
        fields: tuple[FieldObservation, ...] = ()
        if unit.evidence is not None and unit.route in FIELD_ROUTES:
            evidence = await load_model(services, unit.evidence, EvidenceUnit)
            fields = evidence.fields
        units[unit.unit_id] = UnitMetadata(route=unit.route, kind=unit.kind, fields=fields)
    return manifest.family, units, manifest.document_codes


async def projection_rows(
    services: StageServices,
    record: EvidenceRecord,
    placements: Sequence[SourceSnapshot],
    settings: ProjectionSettings,
    *,
    model: str,
    dimensions: int,
) -> dict[str, dict[str, Any]]:
    """Rows of one embedded evidence by key; ``placements`` in canonical order."""

    family, units, document_codes = await evidence_units(services, record)
    chunks = await load_jsonl(services, record.chunk_set, EvidenceChunk)
    vectors = await services.require_embedding_cache().get_many(
        sorted({chunk.text_sha256 for chunk in chunks}), model=model, dimensions=dimensions
    )
    if any(chunk.text_sha256 not in vectors for chunk in chunks):
        raise IntegrityError(f"Embedding cache misses vectors of {record.evidence_id}")
    return {
        row[KEY_FIELD]: row
        for row in (
            build_row(
                chunk,
                vectors[chunk.text_sha256],
                placements=placements,
                family=family,
                settings=settings,
                units=units,
                document_codes=document_codes,
            )
            for chunk in chunks
        )
    }


@dataclass(frozen=True)
class ProjectSearchStage:
    settings: ProjectionSettings

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def _evidence(
        self, context: StageContext, inputs: ProjectInputs
    ) -> EvidenceRecord | None:
        repository = context.services.require_evidence()
        if inputs.evidence_id is not None:
            return await repository.get_document(inputs.evidence_id)
        return await repository.latest_document(
            inputs.content_hash, inputs.policy_version, status=EvidenceStatus.EMBEDDED
        )

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(ProjectInputs, job)
        services = context.services
        settings = self.settings.for_policy(inputs.policy_version)
        snapshots = services.require_snapshots()
        refresh = await self._left_behind(context, inputs)
        placements = canonical_order(await snapshots.for_content(inputs.content_hash))
        now = services.clock.now()
        if not placements:
            retired, unretired = await self._retire(context, settings, inputs.content_hash, set())
            if unretired:
                return self._wait(settings, now, f"{unretired} row(s) could not be retired yet")
            return StageOutcome.succeeded(
                next_jobs=refresh, checkpoint={"placements": 0, "retired": retired}
            )
        record = await self._evidence(context, inputs)
        if record is None or record.status is not EvidenceStatus.EMBEDDED:
            return StageOutcome.succeeded(
                next_jobs=refresh, checkpoint={"skipped": "no embedded evidence yet"}
            )
        provider = services.require_embeddings()
        rows = await projection_rows(
            services,
            record,
            placements,
            settings,
            model=provider.model,
            dimensions=provider.dimensions,
        )
        repository = services.require_projections()
        await repository.upsert(
            [
                ProjectionItem(
                    store=settings.store,
                    target=settings.target,
                    projection_id=key,
                    release_id=settings.release_id,
                    evidence_id=record.evidence_id,
                    chunk_id=row["chunk_id"],
                    content_hash=inputs.content_hash,
                    placement_id=row["placement_id"],
                    payload_sha256=row[DIGEST_FIELD],
                )
                for key, row in rows.items()
            ],
            now=now,
        )
        items = {
            item.projection_id: item
            for item in await repository.items(settings.store, settings.target, list(rows))
        }
        projector = services.require_projector()
        live_or_failed = LIVE_STATES | {ProjectionState.FAILED_PERMANENT}
        upload = [rows[key] for key, item in items.items() if item.state not in live_or_failed]
        failed = {
            key: item.last_error or "failed"
            for key, item in items.items()
            if item.state is ProjectionState.FAILED_PERMANENT
        }
        retryable: dict[str, str] = {}
        if upload:
            report = await projector.upload(upload, key_field=KEY_FIELD)
            states: dict[str, tuple[ProjectionState, str | None]] = {
                key: (ProjectionState.ACKNOWLEDGED, None) for key in report.accepted
            }
            for key, why in report.permanent.items():
                states[key] = (ProjectionState.FAILED_PERMANENT, why)
            for key, why in report.retryable.items():
                states[key] = (ProjectionState.PENDING, why)
            await repository.set_state(
                settings.store,
                settings.target,
                states,
                now=services.clock.now(),
                count_attempt=True,
            )
            failed.update(report.permanent)
            retryable.update(report.retryable)
        unverified = {
            key: rows[key][DIGEST_FIELD]
            for key, item in items.items()
            if key not in failed
            and key not in retryable
            and item.state is not ProjectionState.VERIFIED
        }
        problems = (
            await projector.verify(unverified, key_field=KEY_FIELD, digest_field=DIGEST_FIELD)
            if unverified
            else {}
        )
        verified: dict[str, tuple[ProjectionState, str | None]] = {
            key: (ProjectionState.VERIFIED, None) for key in unverified if key not in problems
        }
        if verified:
            await repository.set_state(
                settings.store, settings.target, verified, now=services.clock.now()
            )
        counts = {
            "rows": len(rows),
            "uploaded": len(upload),
            "verified": len(verified),
            "failed": len(failed),
            "pending": len(retryable) + len(problems),
        }
        if failed:
            return StageOutcome(
                status=JobStatus.NEEDS_REVIEW,
                reason=f"{len(failed)} row(s) were rejected permanently by the index",
                error_category=ErrorCategory.INVALID_INPUT,
                checkpoint=counts,
                next_jobs=refresh,
            )
        if retryable or problems:
            started = str(job.checkpoint.get("started_at") or now.isoformat())
            elapsed = (now - datetime.fromisoformat(started)).total_seconds()
            if elapsed > settings.verify_deadline_seconds:
                return StageOutcome(
                    status=JobStatus.NEEDS_REVIEW,
                    reason=f"{counts['pending']} row(s) not visible after {int(elapsed)}s",
                    error_category=ErrorCategory.TIMEOUT,
                    checkpoint=counts,
                    next_jobs=refresh,
                )
            return StageOutcome(
                status=JobStatus.RETRY_WAIT,
                reason=f"{counts['pending']} row(s) pending or not yet visible",
                error_category=ErrorCategory.TRANSIENT,
                not_before=now + timedelta(seconds=settings.retry_seconds),
                retry_after_seconds=settings.retry_seconds,
                checkpoint={**counts, "started_at": started},
            )
        # A placement added or moved while this job ran: project again from the current set.
        current = await snapshots.for_content(inputs.content_hash)
        if {s.snapshot_id for s in current} != {s.snapshot_id for s in placements}:
            return self._wait(settings, now, "placements changed during projection", counts)
        retired, unretired = await self._retire(context, settings, inputs.content_hash, set(rows))
        if unretired:
            return self._wait(
                settings,
                now,
                f"{unretired} older row(s) could not be retired yet",
                {**counts, "retired": retired},
            )
        return StageOutcome.succeeded(
            next_jobs=refresh,
            checkpoint={**counts, "placements": len(placements), "retired": retired},
        )

    @staticmethod
    def _wait(
        settings: ProjectionSettings,
        now: datetime,
        reason: str,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return StageOutcome(
            status=JobStatus.RETRY_WAIT,
            reason=reason,
            error_category=ErrorCategory.TRANSIENT,
            not_before=now + timedelta(seconds=settings.retry_seconds),
            retry_after_seconds=settings.retry_seconds,
            checkpoint=dict(checkpoint) if checkpoint else None,
        )

    async def _left_behind(
        self, context: StageContext, inputs: ProjectInputs
    ) -> tuple[JobSpec, ...]:
        """When a placement now holds this content, refresh the contents it held before."""

        if inputs.trigger_snapshot_id is None:
            return ()
        snapshots = context.services.require_snapshots()
        trigger = await snapshots.get(inputs.trigger_snapshot_id)
        # A refresh job (triggered by another content's snapshot) never fans out again.
        if trigger is None or trigger.content_hash != inputs.content_hash:
            return ()
        previous = await snapshots.contents_for_placement(trigger.placement_id)
        return tuple(
            project_job(
                context.services.planner,
                content_hash=content_hash,
                policy_version=inputs.policy_version,
                trigger_snapshot_id=trigger.snapshot_id,
            )
            for content_hash in previous
            if content_hash != inputs.content_hash
        )

    async def _retire(
        self,
        context: StageContext,
        settings: ProjectionSettings,
        content_hash: str,
        current: set[str],
    ) -> tuple[int, int]:
        """Delete this release's other rows of the content; return (retired, still pending)."""

        services = context.services
        repository = services.require_projections()
        active = await repository.active_for_content(settings.store, settings.target, content_hash)
        stale = [
            item.projection_id
            for item in active
            if item.release_id == settings.release_id and item.projection_id not in current
        ]
        if not stale:
            return 0, 0
        report = await services.require_projector().delete(stale, key_field=KEY_FIELD)
        if report.accepted:
            await repository.set_state(
                settings.store,
                settings.target,
                {key: (ProjectionState.RETIRED, None) for key in report.accepted},
                now=services.clock.now(),
            )
        return len(report.accepted), len(stale) - len(report.accepted)
