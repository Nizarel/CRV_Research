"""Copy one Search release into ``crv_retrieval.chunk`` from the cached evidence.

No paid call is made: rows are rebuilt by the projection stage's own code from the evidence the
Search rows came from, with vectors from the embedding cache. Every rebuilt digest is compared
with the Search row's; drift (projection code or placements changed since the upload) skips
the content unless allowed. A copied row counts once it is read back with its digest, and is
recorded in ``ingest.projection_item`` so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..adapters.postgres.pool import PgPool
from ..adapters.postgres.retrieval import (
    STORE,
    TARGET,
    PendingContent,
    PostgresRetrievalProjector,
    pending_contents,
)
from ..domain.errors import IntegrityError
from ..domain.projection import ProjectionItem, ProjectionState
from ..stages.base import StageServices
from ..stages.project_search import (
    DIGEST_FIELD,
    KEY_FIELD,
    ProjectionSettings,
    canonical_order,
    projection_rows,
)

MAX_PROBLEMS = 50


@dataclass
class CopyReport:
    release_id: str
    dry_run: bool
    contents: int = 0
    rows: int = 0
    identical_rows: int = 0
    copied_rows: int = 0
    drifted_contents: int = 0
    skipped_contents: int = 0
    failed_contents: int = 0
    missing_rows: int = 0
    """Search rows the rebuild no longer produces."""
    extra_rows: int = 0
    """Rebuilt rows Search does not have."""
    digest_mismatches: int = 0
    seconds: float = 0.0
    rows_per_second: float = 0.0
    problems: list[dict[str, Any]] = field(default_factory=list)

    def note(self, content: PendingContent, problem: str, **details: Any) -> None:
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append({"content_hash": content.content_hash, "problem": problem,
                                  **details})


@dataclass(frozen=True)
class CopyOptions:
    source_store: str
    source_target: str
    model: str
    dimensions: int
    limit: int
    concurrency: int = 4
    dry_run: bool = False
    allow_drift: bool = False
    refresh: bool = False


async def copy_release(
    services: StageServices, pool: PgPool, settings: ProjectionSettings, options: CopyOptions
) -> CopyReport:
    started = time.perf_counter()
    report = CopyReport(release_id=settings.release_id, dry_run=options.dry_run)
    pending = await pending_contents(
        pool,
        source_store=options.source_store,
        source_target=options.source_target,
        release_id=settings.release_id,
        limit=options.limit,
        refresh=options.refresh,
    )
    projector = PostgresRetrievalProjector(pool, services.clock)
    gate = asyncio.Semaphore(options.concurrency)

    async def one(content: PendingContent) -> None:
        async with gate:
            try:
                await _copy_content(services, projector, settings, options, content, report)
            except Exception as error:  # recorded per content; the run continues
                report.failed_contents += 1
                report.note(content, "failed", error=f"{type(error).__name__}: {error}"[:500])

    await asyncio.gather(*(one(content) for content in pending))
    report.contents = len(pending)
    report.seconds = round(time.perf_counter() - started, 1)
    report.rows_per_second = round(report.rows / report.seconds, 1) if report.seconds else 0.0
    return report


async def _copy_content(
    services: StageServices,
    projector: PostgresRetrievalProjector,
    settings: ProjectionSettings,
    options: CopyOptions,
    content: PendingContent,
    report: CopyReport,
) -> None:
    record = await services.require_evidence().get_document(content.evidence_id)
    if record is None:
        raise IntegrityError(f"Evidence {content.evidence_id} is not recorded")
    placements = canonical_order(await services.require_snapshots().for_content(
        content.content_hash
    ))
    if not placements:
        report.skipped_contents += 1
        report.note(content, "no_current_placement")
        return
    rows = await projection_rows(
        services, record, placements, settings, model=options.model, dimensions=options.dimensions
    )
    built = {key: str(row[DIGEST_FIELD]) for key, row in rows.items()}
    missing = sorted(set(content.expected) - set(built))
    extra = sorted(set(built) - set(content.expected))
    mismatched = sorted(
        key for key in set(built) & set(content.expected) if built[key] != content.expected[key]
    )
    report.rows += len(rows)
    report.identical_rows += len(built) - len(extra) - len(mismatched)
    report.missing_rows += len(missing)
    report.extra_rows += len(extra)
    report.digest_mismatches += len(mismatched)
    if missing or extra or mismatched:
        report.drifted_contents += 1
        report.note(content, "drift", missing=len(missing), extra=len(extra),
                    mismatched=len(mismatched), sample=(mismatched or missing or extra)[:3])
        if not options.allow_drift:
            report.skipped_contents += 1
            return
    if options.dry_run:
        return

    repository = services.require_projections()
    await repository.upsert(
        [
            ProjectionItem(
                store=STORE,
                target=TARGET,
                projection_id=key,
                release_id=settings.release_id,
                evidence_id=record.evidence_id,
                chunk_id=row["chunk_id"],
                content_hash=content.content_hash,
                placement_id=row["placement_id"],
                payload_sha256=row[DIGEST_FIELD],
            )
            for key, row in rows.items()
        ],
        now=services.clock.now(),
    )
    outcome = await projector.upload(list(rows.values()), key_field=KEY_FIELD)
    problems = await projector.verify(
        {key: built[key] for key in outcome.accepted}, key_field=KEY_FIELD,
        digest_field=DIGEST_FIELD,
    )
    states: dict[str, tuple[ProjectionState, str | None]] = {
        key: (ProjectionState.VERIFIED, None) for key in outcome.accepted if key not in problems
    }
    states.update({key: (ProjectionState.PENDING, why) for key, why in problems.items()})
    states.update(
        {key: (ProjectionState.FAILED_PERMANENT, why) for key, why in outcome.permanent.items()
         if key in rows}
    )
    await repository.set_state(STORE, TARGET, states, now=services.clock.now(),
                               count_attempt=True)
    report.copied_rows += sum(state is ProjectionState.VERIFIED for state, _ in states.values())
    if problems or outcome.permanent:
        report.note(content, "not_copied", unverified=len(problems),
                    rejected=len(outcome.permanent))
