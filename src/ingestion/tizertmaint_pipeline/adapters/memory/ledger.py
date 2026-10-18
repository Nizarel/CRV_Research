"""In-memory ``JobLedger``, ``Outbox`` and ``SnapshotRepository`` with PostgreSQL semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.budgets import freed_submit_stage
from ...domain.errors import ErrorCategory, IntegrityError, InvalidInputError, LeaseLostError
from ...domain.ids import new_event_id, new_run_id
from ...domain.models import (
    ArtifactRef,
    AttemptRecord,
    JobSpec,
    Lease,
    OutboxEvent,
    PlanRecord,
    ProviderUsage,
    SourceSnapshot,
    StageJob,
    StageOutcome,
    UnitDisposition,
    UnitState,
    truncate_error,
)
from ...domain.states import (
    ACTIVE_STATUSES,
    CLAIMABLE_STATUSES,
    REMOTE_POLLS,
    REPLAY_TARGETS,
    RESCHEDULED_STATUSES,
    WORK_IN_PROGRESS,
    JobStatus,
    StageName,
    require_transition,
    settled_unit_message,
)
from ...ports.ledger import ActiveWork, RecoveryResult


@dataclass
class _OutboxRow:
    event: OutboxEvent
    claimed_until: datetime | None = None
    sent_at: datetime | None = None
    send_attempts: int = 0
    last_error: str | None = None


class InMemoryLedger:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, StageJob] = {}
        self._attempts: dict[str, AttemptRecord] = {}
        self._artifacts: dict[str, ArtifactRef] = {}
        self._job_artifacts: set[tuple[str, str, str]] = set()
        self._outbox: dict[str, _OutboxRow] = {}
        self._plans: dict[str, PlanRecord] = {}
        self._units: dict[str, UnitState] = {}
        self._barriers: dict[str, dict[str, datetime | None]] = {}
        self.replays: list[tuple[str, JobStatus, JobStatus, str, str, datetime]] = []

    # Inspection helpers for tests and local dry runs.
    def jobs(self) -> list[StageJob]:
        return list(self._jobs.values())

    def outbox_rows(self) -> list[_OutboxRow]:
        return list(self._outbox.values())

    def artifacts(self) -> dict[str, ArtifactRef]:
        return dict(self._artifacts)

    def job_artifacts(self, job_id: str) -> set[tuple[str, str]]:
        return {(role, sha) for job, role, sha in self._job_artifacts if job == job_id}

    def _new_event(self, job: StageJob, available_at: datetime) -> None:
        event_id = new_event_id()
        self._outbox[event_id] = _OutboxRow(
            OutboxEvent(
                event_id=event_id,
                job_id=job.job_id,
                pool=job.pool,
                envelope=job.envelope(event_id),
                available_at=available_at,
            )
        )

    def _insert(self, spec: JobSpec, *, now: datetime, parent_job_id: str | None) -> str | None:
        if spec.job_id in self._jobs:
            return None
        blocked = spec.barrier_plan_id is not None
        if blocked and spec.barrier_plan_id not in self._plans:
            raise IntegrityError(f"Barrier plan {spec.barrier_plan_id} is not registered")
        job = StageJob(
            job_id=spec.job_id,
            unit_id=spec.unit_id,
            stage=spec.stage,
            pool=spec.pool,
            fingerprint=spec.fingerprint,
            status=JobStatus.BLOCKED if blocked else JobStatus.PENDING,
            priority=spec.priority,
            inputs=dict(spec.inputs),
            checkpoint={},
            source_snapshot_id=spec.source_snapshot_id,
            content_hash=spec.content_hash,
            parent_job_id=parent_job_id,
            not_before=spec.not_before or now,
            attempt=0,
            failure_count=0,
            max_failures=spec.max_failures,
            fencing_token=0,
            lease_owner=None,
            lease_expires_at=None,
            current_run_id=None,
            last_error_category=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.job_id] = job
        if spec.barrier_plan_id is not None:
            self._barriers.setdefault(spec.barrier_plan_id, {})[job.job_id] = None
        else:
            self._new_event(job, job.not_before)
        return job.job_id

    def _register_plan(self, plan: PlanRecord, now: datetime) -> None:
        if plan.plan_id in self._plans:
            return
        self._plans[plan.plan_id] = plan
        for unit in plan.units:
            self._units.setdefault(
                unit.unit_id,
                UnitState(
                    unit=unit,
                    plan_id=plan.plan_id,
                    disposed_at=now if unit.disposition is not None else None,
                ),
            )

    def _settle_unit(
        self,
        unit_id: str,
        disposition: UnitDisposition,
        reason: str | None,
        evidence: ArtifactRef | None,
        now: datetime,
    ) -> str | None:
        state = self._units.get(unit_id)
        if state is None or state.disposition is not None:
            return None
        self._units[unit_id] = state.model_copy(
            update={
                "unit": state.unit.model_copy(
                    update={"disposition": disposition, "reason": reason}
                ),
                "evidence": evidence,
                "disposed_at": now,
            }
        )
        return state.plan_id

    def _release(self, plan_id: str, now: datetime) -> int:
        if any(
            state.disposition is None
            for state in self._units.values()
            if state.plan_id == plan_id
        ):
            return 0
        released = 0
        barrier = self._barriers.get(plan_id, {})
        for job_id, released_at in barrier.items():
            job = self._jobs[job_id]
            if released_at is not None or job.status is not JobStatus.BLOCKED:
                continue
            pending = job.model_copy(
                update={"status": JobStatus.PENDING, "not_before": now, "updated_at": now}
            )
            self._jobs[job_id] = pending
            barrier[job_id] = now
            self._new_event(pending, now)
            released += 1
        return released

    async def enqueue(self, specs: Sequence[JobSpec], *, now: datetime) -> list[str]:
        async with self._lock:
            inserted = [self._insert(spec, now=now, parent_job_id=None) for spec in specs]
            return [job_id for job_id in inserted if job_id is not None]

    async def get(self, job_id: str) -> StageJob | None:
        return self._jobs.get(job_id)

    async def attempts(self, job_id: str) -> list[AttemptRecord]:
        records = [record for record in self._attempts.values() if record.job_id == job_id]
        return sorted(records, key=lambda record: record.attempt)

    async def claim(
        self, job_id: str, *, owner: str, lease_seconds: float, now: datetime
    ) -> Lease | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in CLAIMABLE_STATUSES or job.not_before > now:
                return None
            run_id = new_run_id()
            leased = job.model_copy(
                update={
                    "status": JobStatus.LEASED,
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "fencing_token": job.fencing_token + 1,
                    "attempt": job.attempt + 1,
                    "current_run_id": run_id,
                    "updated_at": now,
                }
            )
            self._jobs[job_id] = leased
            self._attempts[run_id] = AttemptRecord(
                run_id=run_id,
                job_id=job_id,
                attempt=leased.attempt,
                fencing_token=leased.fencing_token,
                worker=owner,
                started_at=now,
            )
            return Lease(job=leased, run_id=run_id)

    def _current(self, lease: Lease) -> StageJob:
        job = self._jobs.get(lease.job.job_id)
        if (
            job is None
            or job.fencing_token != lease.fencing_token
            or job.status not in ACTIVE_STATUSES
        ):
            raise LeaseLostError(f"Lease lost for job {lease.job.job_id}")
        return job

    async def heartbeat(self, lease: Lease, *, lease_seconds: float, now: datetime) -> Lease:
        async with self._lock:
            job = self._current(lease)
            renewed = job.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self._jobs[job.job_id] = renewed
            return Lease(job=renewed, run_id=lease.run_id)

    async def complete(self, lease: Lease, outcome: StageOutcome, *, now: datetime) -> None:
        async with self._lock:
            job = self._current(lease)
            require_transition(job.status, outcome.status)
            for link in outcome.artifacts:
                known = self._artifacts.get(link.artifact.sha256)
                if known is not None and known.size_bytes != link.artifact.size_bytes:
                    raise IntegrityError(f"Artifact {link.artifact.sha256} size conflict")

            self._jobs[job.job_id] = job.model_copy(
                update={
                    "status": outcome.status,
                    "not_before": outcome.not_before or job.not_before,
                    "checkpoint": (
                        outcome.checkpoint if outcome.checkpoint is not None else job.checkpoint
                    ),
                    "failure_count": job.failure_count + (1 if outcome.counts_as_failure else 0),
                    "last_error_category": outcome.error_category,
                    "last_error": outcome.reason,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "current_run_id": None,
                    "updated_at": now,
                }
            )
            self._attempts[lease.run_id] = self._attempts[lease.run_id].model_copy(
                update={
                    "finished_at": now,
                    "outcome": outcome.status,
                    "error_category": outcome.error_category,
                    "error": outcome.reason,
                    "operation": outcome.operation,
                    "usage": outcome.usage,
                }
            )
            for link in outcome.artifacts:
                self._artifacts.setdefault(link.artifact.sha256, link.artifact)
                self._job_artifacts.add((job.job_id, link.role, link.artifact.sha256))
            affected: set[str] = set()
            if outcome.plan is not None:
                self._register_plan(outcome.plan, now)
                affected.add(outcome.plan.plan_id)
            settled = outcome.settles_unit()
            if settled is not None:
                plan_id = self._settle_unit(job.unit_id, *settled, now)
                if plan_id is not None:
                    affected.add(plan_id)
            for spec in outcome.next_jobs:
                self._insert(spec, now=now, parent_job_id=job.job_id)
                if spec.barrier_plan_id is not None:
                    affected.add(spec.barrier_plan_id)
            for plan_id in sorted(affected):
                self._release(plan_id, now)
            if outcome.status in RESCHEDULED_STATUSES and outcome.not_before is not None:
                self._new_event(self._jobs[job.job_id], outcome.not_before)
            freed = freed_submit_stage(job.stage, outcome)
            if freed is not None:
                self._wake(freed, 1, now)

    def _parked(self, submit: StageName) -> list[StageJob]:
        return [
            job
            for job in self._jobs.values()
            if job.stage is submit
            and job.status is JobStatus.RETRY_WAIT
            and job.last_error_category is ErrorCategory.SLOT_WAIT
        ]

    def _wake(self, submit: StageName, count: int, now: datetime) -> int:
        waiting = sorted(
            (job for job in self._parked(submit) if job.not_before > now),
            key=lambda job: (job.priority, job.created_at, job.job_id),
        )[: max(count, 0)]
        for job in waiting:
            woken = job.model_copy(update={"not_before": now, "updated_at": now})
            self._jobs[job.job_id] = woken
            self._new_event(woken, now)
        return len(waiting)

    async def wake_parked(self, submit: StageName, *, limit: int, now: datetime) -> int:
        async with self._lock:
            polls = REMOTE_POLLS[submit]
            in_flight = sum(
                (job.stage is polls and job.status in WORK_IN_PROGRESS)
                or (job.stage is submit and job.status is JobStatus.RUNNING)
                for job in self._jobs.values()
            )
            due = sum(job.not_before <= now for job in self._parked(submit))
            return self._wake(submit, limit - in_flight - due, now)

    async def plan(self, plan_id: str) -> PlanRecord | None:
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        units = tuple(state.unit for state in await self.plan_units(plan_id))
        return plan.model_copy(update={"units": units})

    async def plan_units(self, plan_id: str) -> list[UnitState]:
        states = [state for state in self._units.values() if state.plan_id == plan_id]
        return sorted(states, key=lambda state: state.unit.ordinal)

    async def replay(self, job_id: str, *, actor: str, reason: str, now: datetime) -> StageJob:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise InvalidInputError(f"Unknown job {job_id}")
            target = REPLAY_TARGETS.get(job.status)
            if target is None:
                raise InvalidInputError(f"Job {job_id} is {job.status.value}; not replayable")
            failed = job.status is JobStatus.FAILED_PERMANENT
            if failed and job.unit_id in self._units:
                raise InvalidInputError(settled_unit_message(job_id, job.unit_id))
            require_transition(job.status, target)
            note = truncate_error(f"replayed by {actor}: {reason}")
            replayed = job.model_copy(
                update={
                    "status": target,
                    "not_before": now,
                    "last_error": note,
                    "updated_at": now,
                    "failure_count": 0 if failed else job.failure_count,
                }
            )
            self._jobs[job_id] = replayed
            self.replays.append((job_id, job.status, target, actor, reason, now))
            self._new_event(replayed, now)
            return replayed

    async def active_work(self, stage: StageName) -> ActiveWork:
        return self._work(stage, lambda job: True)

    async def account_work(self, stage: StageName, account: str | None) -> ActiveWork:
        return self._work(stage, lambda job: job.inputs.get("account") == account)

    def _work(self, stage: StageName, keep: Callable[[StageJob], bool]) -> ActiveWork:
        active = [
            job
            for job in self._jobs.values()
            if job.stage is stage and job.status in WORK_IN_PROGRESS and keep(job)
        ]
        pages = sum(int(job.inputs.get("billable_pages", 0) or 0) for job in active)
        leased = sum(job.status in ACTIVE_STATUSES for job in active)
        running = sum(job.status is JobStatus.RUNNING for job in active)
        return ActiveWork(jobs=len(active), billable_pages=pages, leased=leased, running=running)

    async def start_remote(
        self,
        lease: Lease,
        *,
        polls: StageName,
        limit: int,
        account: str | None = None,
        now: datetime,
    ) -> bool:
        async with self._lock:
            job = self._current(lease)
            in_flight = sum(
                (
                    other.stage is polls
                    and other.status in WORK_IN_PROGRESS
                    and other.inputs.get("account") == account
                )
                or (
                    other.stage is job.stage
                    and other.status is JobStatus.RUNNING
                    and other.job_id != job.job_id
                    and other.checkpoint.get("account") == account
                )
                for other in self._jobs.values()
            )
            if in_flight >= limit:
                return False
            self._jobs[job.job_id] = job.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "checkpoint": {**job.checkpoint, "account": account},
                    "updated_at": now,
                }
            )
            return True

    async def account_spend(self, account: str | None, providers: Sequence[str]) -> float:
        return sum(
            record.usage.cost_usd or 0.0
            for record in self._attempts.values()
            if record.usage is not None
            and record.usage.account == account
            and record.usage.provider in providers
        )

    async def provider_pages(self, provider: str) -> int:
        return sum(
            record.usage.pages
            for record in self._attempts.values()
            if record.usage is not None and record.usage.provider == provider
        )

    async def provider_usage(self, provider: str) -> ProviderUsage:
        usages = [
            record.usage
            for record in self._attempts.values()
            if record.usage is not None and record.usage.provider == provider
        ]
        return ProviderUsage(
            provider=provider,
            pages=sum(usage.pages for usage in usages),
            input_tokens=sum(usage.input_tokens for usage in usages),
            output_tokens=sum(usage.output_tokens for usage in usages),
            requests=sum(usage.requests for usage in usages),
        )

    async def release_ready_barriers(self, *, now: datetime, limit: int) -> int:
        async with self._lock:
            released = 0
            for plan_id, barrier in sorted(self._barriers.items()):
                if released >= limit:
                    break
                if any(value is None for value in barrier.values()):
                    released += self._release(plan_id, now)
            return released

    async def recover_expired(self, *, now: datetime, limit: int) -> RecoveryResult:
        async with self._lock:
            expired = sorted(
                (
                    job
                    for job in self._jobs.values()
                    if job.status in ACTIVE_STATUSES
                    and job.lease_expires_at is not None
                    and job.lease_expires_at < now
                ),
                key=lambda job: job.lease_expires_at or now,
            )[:limit]
            requeued = failed = 0
            for job in expired:
                failures = job.failure_count + 1
                status = (
                    JobStatus.FAILED_PERMANENT
                    if failures >= job.max_failures
                    else JobStatus.RETRY_WAIT
                )
                recovered = job.model_copy(
                    update={
                        "status": status,
                        "failure_count": failures,
                        "not_before": now,
                        "last_error_category": ErrorCategory.LEASE_EXPIRED,
                        "last_error": "Lease expired before the attempt committed",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "current_run_id": None,
                        "updated_at": now,
                    }
                )
                self._jobs[job.job_id] = recovered
                if job.current_run_id in self._attempts:
                    self._attempts[job.current_run_id] = self._attempts[
                        job.current_run_id
                    ].model_copy(
                        update={
                            "finished_at": now,
                            "outcome": status,
                            "error_category": ErrorCategory.LEASE_EXPIRED,
                        }
                    )
                if status is JobStatus.RETRY_WAIT:
                    self._new_event(recovered, now)
                    requeued += 1
                else:
                    failed += 1
                    plan_id = self._settle_unit(
                        job.unit_id,
                        UnitDisposition.FAILED,
                        "lease expired too often",
                        None,
                        now,
                    )
                    if plan_id is not None:
                        self._release(plan_id, now)
            return RecoveryResult(requeued=requeued, failed=failed)

    async def redispatch_stale(self, *, now: datetime, stale_before: datetime, limit: int) -> int:
        async with self._lock:
            fresh_jobs = {
                row.event.job_id
                for row in self._outbox.values()
                if row.sent_at is None or row.sent_at > stale_before
            }
            stale = [
                job
                for job in self._jobs.values()
                if job.status in CLAIMABLE_STATUSES
                and job.not_before <= stale_before
                and job.job_id not in fresh_jobs
            ][:limit]
            for job in stale:
                self._new_event(job, now)
            return len(stale)

    async def lease_batch(
        self, *, now: datetime, limit: int, claim_seconds: float
    ) -> list[OutboxEvent]:
        async with self._lock:
            due = sorted(
                (
                    row
                    for row in self._outbox.values()
                    if row.sent_at is None
                    and row.event.available_at <= now
                    and (row.claimed_until is None or row.claimed_until < now)
                ),
                key=lambda row: row.event.available_at,
            )[:limit]
            for row in due:
                row.claimed_until = now + timedelta(seconds=claim_seconds)
                row.send_attempts += 1
            return [row.event for row in due]

    async def mark_sent(self, event_ids: Sequence[str], *, now: datetime) -> None:
        async with self._lock:
            for event_id in event_ids:
                row = self._outbox[event_id]
                row.sent_at, row.claimed_until = now, None

    async def release(self, event_ids: Sequence[str], *, error: str, retry_at: datetime) -> None:
        async with self._lock:
            for event_id in event_ids:
                row = self._outbox[event_id]
                row.claimed_until, row.last_error = None, error
                row.event = row.event.model_copy(update={"available_at": retry_at})


class InMemorySnapshots:
    def __init__(self) -> None:
        self._snapshots: dict[str, SourceSnapshot] = {}

    async def record(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing is None:
            self._snapshots[snapshot.snapshot_id] = snapshot
            return snapshot
        if existing.model_dump(exclude={"observed_at"}) != snapshot.model_dump(
            exclude={"observed_at"}
        ):
            raise IntegrityError(f"Snapshot {snapshot.snapshot_id} conflicts with stored facts")
        return existing

    async def get(self, snapshot_id: str) -> SourceSnapshot | None:
        return self._snapshots.get(snapshot_id)

    async def latest_for_placement(self, placement_id: str) -> SourceSnapshot | None:
        candidates = [s for s in self._snapshots.values() if s.placement_id == placement_id]
        return max(candidates, key=lambda s: (s.observed_at, s.snapshot_id), default=None)

    async def for_content(self, content_hash: str) -> list[SourceSnapshot]:
        placements = {s.placement_id for s in self._snapshots.values()}
        latest = [await self.latest_for_placement(placement) for placement in sorted(placements)]
        return [s for s in latest if s is not None and s.content_hash == content_hash]

    async def contents_for_placement(self, placement_id: str) -> list[str]:
        return sorted(
            {s.content_hash for s in self._snapshots.values() if s.placement_id == placement_id}
        )

    def all(self) -> list[SourceSnapshot]:
        return list(self._snapshots.values())
