"""Durable stage ledger, transactional outbox and source snapshot ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain.models import (
    AttemptRecord,
    JobSpec,
    Lease,
    OutboxEvent,
    PlanRecord,
    ProviderUsage,
    SourceSnapshot,
    StageJob,
    StageOutcome,
    UnitState,
)
from ..domain.states import StageName


@dataclass(frozen=True)
class RecoveryResult:
    requeued: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ActiveWork:
    jobs: int = 0
    billable_pages: int = 0
    """Sum of the ``billable_pages`` input of those jobs (pages already paid or in flight)."""
    leased: int = 0
    """Jobs of the stage holding a lease right now, admitted or not."""
    running: int = 0
    """Submits admitted by ``start_remote`` whose remote operation is being started."""


class PlanReader(Protocol):
    async def plan(self, plan_id: str) -> PlanRecord | None: ...

    async def plan_units(self, plan_id: str) -> list[UnitState]: ...


class WorkloadReader(Protocol):
    async def active_work(self, stage: StageName) -> ActiveWork:
        """Non-terminal jobs of ``stage`` (queued, leased, waiting or retrying)."""
        ...

    async def provider_pages(self, provider: str) -> int:
        """Pages recorded in attempt usage for ``provider`` (paid work)."""
        ...

    async def provider_usage(self, provider: str) -> ProviderUsage:
        """Pages, tokens and requests recorded in attempt usage for ``provider``."""
        ...

    async def account_work(self, stage: StageName, account: str | None) -> ActiveWork:
        """``active_work`` of jobs whose inputs name ``account`` (``None``: this subscription)."""
        ...

    async def account_spend(self, account: str | None, providers: Sequence[str]) -> float:
        """Recorded ``cost_usd`` of ``providers`` usage billed to ``account``."""
        ...

    async def start_remote(
        self,
        lease: Lease,
        *,
        polls: StageName,
        limit: int,
        account: str | None = None,
        now: datetime,
    ) -> bool:
        """Mark the leased submit ``running`` on ``account`` if fewer than ``limit`` of that
        account's remote operations are in flight (unfinished ``polls`` jobs plus the other
        running submits of its stage; ``None`` is this subscription).

        Atomic across workers. Raises ``LeaseLostError`` if admitted with a stale lease."""
        ...


class JobLedger(Protocol):
    """Source of truth for stage work. Queue messages are only wake-ups for these rows."""

    async def enqueue(self, specs: Sequence[JobSpec], *, now: datetime) -> list[str]:
        """Insert jobs idempotently with their outbox events; return IDs actually inserted."""
        ...

    async def get(self, job_id: str) -> StageJob | None: ...

    async def attempts(self, job_id: str) -> list[AttemptRecord]: ...

    async def claim(
        self, job_id: str, *, owner: str, lease_seconds: float, now: datetime
    ) -> Lease | None:
        """Lease a due, claimable job and bump its fencing token, or return ``None``."""
        ...

    async def heartbeat(self, lease: Lease, *, lease_seconds: float, now: datetime) -> Lease:
        """Extend the lease; raises ``LeaseLostError`` if a newer lease owns the job."""
        ...

    async def complete(self, lease: Lease, outcome: StageOutcome, *, now: datetime) -> None:
        """Atomically record the outcome, artifacts, successor jobs and outbox events.

        In the same transaction: register ``outcome.plan`` and its units, settle the job's
        unit (see ``StageOutcome.settles_unit``), release plan barriers whose units all
        settled, and wake one parked submit when the outcome frees a remote slot (see
        ``freed_submit_stage``). Raises ``LeaseLostError`` (and commits nothing) if the fencing
        token is stale.
        """
        ...

    async def plan(self, plan_id: str) -> PlanRecord | None:
        """The registered plan with each unit's current disposition."""
        ...

    async def plan_units(self, plan_id: str) -> list[UnitState]: ...

    async def replay(self, job_id: str, *, actor: str, reason: str, now: datetime) -> StageJob:
        """Audited operator re-run of a ``submission_unknown``, ``needs_review`` or
        ``failed_permanent`` job (see ``REPLAY_TARGETS``).

        Raises ``InvalidInputError`` when the job is unknown, not in a replayable state, or a
        permanent failure that settled a plan unit."""
        ...

    async def release_ready_barriers(self, *, now: datetime, limit: int) -> int:
        """Safety sweep: unblock barrier jobs of fully settled plans; return the count."""
        ...

    async def recover_expired(self, *, now: datetime, limit: int) -> RecoveryResult:
        """Return jobs whose lease expired to ``retry_wait`` (or fail them past budget)."""
        ...

    async def redispatch_stale(self, *, now: datetime, stale_before: datetime, limit: int) -> int:
        """Emit fresh wake-ups for due jobs whose last event was lost or dead-lettered."""
        ...

    async def wake_parked(self, submit: StageName, *, limit: int, now: datetime) -> int:
        """Wake the oldest ``slot_wait`` submits of ``submit`` into its free remote slots.

        Free slots are ``limit`` minus operations in flight minus submits already woken (due)
        but not yet admitted, so a slow queue does not cause over-waking. Returns the count."""
        ...


class Outbox(Protocol):
    async def lease_batch(
        self, *, now: datetime, limit: int, claim_seconds: float
    ) -> list[OutboxEvent]: ...

    async def mark_sent(self, event_ids: Sequence[str], *, now: datetime) -> None: ...

    async def release(
        self, event_ids: Sequence[str], *, error: str, retry_at: datetime
    ) -> None: ...


class SnapshotRepository(Protocol):
    async def record(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        """Insert idempotently; return the stored (possibly pre-existing) snapshot."""
        ...

    async def get(self, snapshot_id: str) -> SourceSnapshot | None: ...

    async def for_content(self, content_hash: str) -> list[SourceSnapshot]:
        """Each placement's latest snapshot, for placements whose latest bytes have this hash."""
        ...

    async def latest_for_placement(self, placement_id: str) -> SourceSnapshot | None: ...

    async def contents_for_placement(self, placement_id: str) -> list[str]:
        """Every content hash ever observed at the placement, sorted."""
        ...
