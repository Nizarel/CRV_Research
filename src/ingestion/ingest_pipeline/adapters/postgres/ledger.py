"""PostgreSQL ``JobLedger``/``Outbox``: SKIP LOCKED claims, fencing and atomic commits."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from ...domain.budgets import freed_submit_stage
from ...domain.envelope import WorkEnvelope
from ...domain.errors import IntegrityError, InvalidInputError, LeaseLostError
from ...domain.ids import new_event_id, new_run_id
from ...domain.models import (
    ArtifactRef,
    AttemptRecord,
    JobSpec,
    Lease,
    OutboxEvent,
    PlanRecord,
    ProviderUsage,
    StageJob,
    StageOutcome,
    UnitDisposition,
    UnitRecord,
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
    WorkerPool,
    require_transition,
    settled_unit_message,
)
from ...ports.ledger import ActiveWork, RecoveryResult
from .pool import PgConnection, PgPool

_COLUMNS = (
    "job_id",
    "unit_id",
    "stage",
    "pool",
    "fingerprint",
    "status",
    "priority",
    "inputs",
    "checkpoint",
    "source_snapshot_id",
    "content_hash",
    "parent_job_id",
    "not_before",
    "attempt",
    "failure_count",
    "max_failures",
    "fencing_token",
    "lease_owner",
    "lease_expires_at",
    "current_run_id",
    "last_error_category",
    "last_error",
    "created_at",
    "updated_at",
)
_JOB = ", ".join(f"job.{column}" for column in _COLUMNS)
_CLAIMABLE = sorted(status.value for status in CLAIMABLE_STATUSES)
_ACTIVE = sorted(status.value for status in ACTIVE_STATUSES)
_IN_PROGRESS = sorted(status.value for status in WORK_IN_PROGRESS)

_SELECT_JOB = f"SELECT {_JOB} FROM ingest.stage_job AS job WHERE job.job_id = $1"

_INSERT_JOB = f"""
INSERT INTO ingest.stage_job AS job (
    job_id, unit_id, stage, fingerprint, pool, status, priority, inputs, source_snapshot_id,
    content_hash, parent_job_id, not_before, max_failures, created_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $14, $6, $7::jsonb, $8, $9, $10, $11, $12, $13, $13)
ON CONFLICT (unit_id, stage, fingerprint) DO NOTHING
RETURNING {_JOB}
"""

_INSERT_BARRIER = """
INSERT INTO ingest.plan_barrier (plan_id, job_id) VALUES ($1, $2) ON CONFLICT DO NOTHING
"""

_INSERT_PLAN = """
INSERT INTO ingest.plan (plan_id, content_hash, snapshot_id, policy_version, family,
                         preflight_sha256, page_count, unit_count, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (plan_id) DO NOTHING
"""

_INSERT_UNIT = """
INSERT INTO ingest.unit (unit_id, plan_id, ordinal, kind, route, pages, locator, signals,
                         disposition, reason, disposed_at, created_at)
VALUES ($1, $2, $3, $4, $5, $6::int[], $7::jsonb, $8::jsonb, $9, $10, $11, $12)
ON CONFLICT (unit_id) DO NOTHING
"""

_SETTLE_UNIT = """
UPDATE ingest.unit
SET disposition = $2, reason = $3, evidence = $4::jsonb, disposed_by_job_id = $5,
    disposed_at = $6
WHERE unit_id = $1 AND disposition IS NULL
RETURNING plan_id
"""

_RELEASE_BARRIER = f"""
WITH ready AS (
    UPDATE ingest.plan_barrier SET released_at = $2
    WHERE plan_id = $1 AND released_at IS NULL
    RETURNING job_id
)
UPDATE ingest.stage_job AS job SET status = 'pending', not_before = $2, updated_at = $2
FROM ready
WHERE job.job_id = ready.job_id AND job.status = 'blocked'
RETURNING {_JOB}
"""

_READY_PLANS = """
SELECT DISTINCT barrier.plan_id FROM ingest.plan_barrier AS barrier
WHERE barrier.released_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM ingest.unit AS unit
      WHERE unit.plan_id = barrier.plan_id AND unit.disposition IS NULL
  )
LIMIT $1
"""

_SELECT_UNITS = """
SELECT unit_id, plan_id, ordinal, kind, route, pages, locator, signals, disposition, reason,
       evidence, disposed_at
FROM ingest.unit WHERE plan_id = $1 ORDER BY ordinal
"""

_INSERT_EVENT = """
INSERT INTO ingest.outbox (event_id, job_id, pool, payload, available_at, created_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6)
"""

_REPLAY = f"""
UPDATE ingest.stage_job AS job
SET status = $2, not_before = $3, last_error = $4, updated_at = $3,
    failure_count = CASE WHEN $5::boolean THEN 0 ELSE job.failure_count END
WHERE job.job_id = $1
RETURNING {_JOB}
"""

_INSERT_REPLAY = """
INSERT INTO ingest.job_replay (job_id, from_status, to_status, actor, reason, replayed_at)
VALUES ($1, $2, $3, $4, $5, $6)
"""

_CLAIM = f"""
WITH candidate AS (
    SELECT job_id FROM ingest.stage_job
    WHERE job_id = $1 AND status = ANY($2::text[]) AND not_before <= $3
    FOR UPDATE SKIP LOCKED
)
UPDATE ingest.stage_job AS job
SET status = 'leased', lease_owner = $4, lease_expires_at = $5, current_run_id = $6,
    fencing_token = job.fencing_token + 1, attempt = job.attempt + 1, updated_at = $3
FROM candidate
WHERE job.job_id = candidate.job_id
RETURNING {_JOB}
"""

_INSERT_ATTEMPT = """
INSERT INTO ingest.stage_attempt (run_id, job_id, attempt, fencing_token, worker, started_at)
VALUES ($1, $2, $3, $4, $5, $6)
"""

_HEARTBEAT = f"""
UPDATE ingest.stage_job AS job SET lease_expires_at = $3, updated_at = $4
WHERE job.job_id = $1 AND job.fencing_token = $2 AND job.status = ANY($5::text[])
RETURNING {_JOB}
"""

_COMPLETE = f"""
UPDATE ingest.stage_job AS job
SET status = $3,
    not_before = COALESCE($4, job.not_before),
    checkpoint = COALESCE($5::jsonb, job.checkpoint),
    failure_count = job.failure_count + $6,
    last_error_category = $7,
    last_error = $8,
    lease_owner = NULL, lease_expires_at = NULL, current_run_id = NULL,
    updated_at = $9
WHERE job.job_id = $1 AND job.fencing_token = $2 AND job.status = ANY($10::text[])
RETURNING {_JOB}
"""

_FINISH_ATTEMPT = """
UPDATE ingest.stage_attempt
SET finished_at = $2, outcome = $3, error_category = $4, error = $5,
    retry_after_seconds = $6, operation = $7::jsonb, usage = $8::jsonb
WHERE run_id = $1
"""

_INSERT_ARTIFACT = """
INSERT INTO ingest.artifact (sha256, uri, size_bytes, media_type, schema_name, producer_run_id,
                             created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (sha256) DO NOTHING
"""

_LINK_ARTIFACT = """
INSERT INTO ingest.job_artifact (job_id, role, sha256, run_id) VALUES ($1, $2, $3, $4)
ON CONFLICT DO NOTHING
"""

_RECOVER = f"""
WITH expired AS (
    SELECT job_id, current_run_id AS expired_run_id FROM ingest.stage_job
    WHERE status = ANY($3::text[]) AND lease_expires_at < $1
    ORDER BY lease_expires_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE ingest.stage_job AS job
SET status = CASE WHEN job.failure_count + 1 >= job.max_failures
                  THEN 'failed_permanent' ELSE 'retry_wait' END,
    failure_count = job.failure_count + 1,
    not_before = $1,
    last_error_category = 'lease_expired',
    last_error = 'Lease expired before the attempt committed',
    lease_owner = NULL, lease_expires_at = NULL, current_run_id = NULL,
    updated_at = $1
FROM expired
WHERE job.job_id = expired.job_id
RETURNING {_JOB}, expired.expired_run_id
"""

# $5 is the billing account; NULL (and a missing key) is this subscription.
_REMOTE_IN_FLIGHT = """
SELECT (SELECT count(*) FROM ingest.stage_job
        WHERE stage = $1 AND status = ANY($2::text[])
          AND inputs->>'account' IS NOT DISTINCT FROM $5::text)
     + (SELECT count(*) FROM ingest.stage_job
        WHERE stage = $3 AND status = 'running' AND job_id <> $4
          AND checkpoint->>'account' IS NOT DISTINCT FROM $5::text)
"""

_REMOTE_IN_FLIGHT_ALL = """
SELECT (SELECT count(*) FROM ingest.stage_job WHERE stage = $1 AND status = ANY($2::text[]))
     + (SELECT count(*) FROM ingest.stage_job WHERE stage = $3 AND status = 'running')
"""

_START_REMOTE = """
UPDATE ingest.stage_job
SET status = 'running', updated_at = $3,
    checkpoint = checkpoint || jsonb_build_object('account', $4::text)
WHERE job_id = $1 AND fencing_token = $2 AND status IN ('leased', 'running')
RETURNING job_id
"""

# Literal status/category so the planner can use stage_job_parked_idx (migration 0009).
_WAKE_PARKED = f"""
WITH next AS (
    SELECT job_id FROM ingest.stage_job
    WHERE stage = $1 AND status = 'retry_wait' AND last_error_category = 'slot_wait'
      AND not_before > $2
    ORDER BY priority, created_at, job_id
    LIMIT $3
    FOR UPDATE SKIP LOCKED
)
UPDATE ingest.stage_job AS job SET not_before = $2, updated_at = $2
FROM next
WHERE job.job_id = next.job_id
RETURNING {_JOB}
"""

_PARKED_DUE = """
SELECT count(*) FROM ingest.stage_job
WHERE stage = $1 AND status = 'retry_wait' AND last_error_category = 'slot_wait'
  AND not_before <= $2
"""

_EXPIRE_ATTEMPT = """
UPDATE ingest.stage_attempt SET finished_at = $2, outcome = $3, error_category = 'lease_expired'
WHERE run_id = $1
"""

_STALE = f"""
SELECT {_JOB} FROM ingest.stage_job AS job
WHERE job.status = ANY($3::text[]) AND job.not_before <= $1
  AND NOT EXISTS (
      SELECT 1 FROM ingest.outbox AS event
      WHERE event.job_id = job.job_id AND (event.sent_at IS NULL OR event.sent_at > $1)
  )
ORDER BY job.not_before
LIMIT $2
FOR UPDATE OF job SKIP LOCKED
"""

_LEASE_EVENTS = """
WITH batch AS (
    SELECT event_id FROM ingest.outbox
    WHERE sent_at IS NULL AND available_at <= $1
      AND (claimed_until IS NULL OR claimed_until < $1)
    ORDER BY available_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE ingest.outbox AS event
SET claimed_until = $3, send_attempts = event.send_attempts + 1
FROM batch
WHERE event.event_id = batch.event_id
RETURNING event.event_id, event.job_id, event.pool, event.payload, event.available_at
"""


def _job(row: Any) -> StageJob:
    return StageJob.model_validate({column: row[column] for column in _COLUMNS})


def _unit_state(row: Any) -> UnitState:
    disposition = row["disposition"]
    return UnitState(
        unit=UnitRecord(
            unit_id=row["unit_id"],
            ordinal=row["ordinal"],
            kind=row["kind"],
            route=row["route"],
            pages=tuple(row["pages"] or ()),
            locator=row["locator"] or {},
            signals=row["signals"] or {},
            disposition=UnitDisposition(disposition) if disposition else None,
            reason=row["reason"],
        ),
        plan_id=row["plan_id"],
        evidence=ArtifactRef.model_validate(row["evidence"]) if row["evidence"] else None,
        disposed_at=row["disposed_at"],
    )


def _dump(model: Any) -> dict[str, Any] | None:
    return None if model is None else model.model_dump(mode="json")


class PostgresLedger:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def _event(
        self, connection: PgConnection, job: StageJob, *, available_at: datetime, now: datetime
    ) -> None:
        event_id = new_event_id()
        payload = job.envelope(event_id).model_dump(mode="json", exclude_none=True)
        await connection.execute(
            _INSERT_EVENT, event_id, job.job_id, job.pool.value, payload, available_at, now
        )

    async def _insert(
        self,
        connection: PgConnection,
        spec: JobSpec,
        *,
        now: datetime,
        parent_job_id: str | None,
    ) -> str | None:
        row = await connection.fetchrow(
            _INSERT_JOB,
            spec.job_id,
            spec.unit_id,
            spec.stage.value,
            spec.fingerprint,
            spec.pool.value,
            spec.priority,
            spec.inputs,
            spec.source_snapshot_id,
            spec.content_hash,
            parent_job_id,
            spec.not_before or now,
            spec.max_failures,
            now,
            (JobStatus.BLOCKED if spec.barrier_plan_id else JobStatus.PENDING).value,
        )
        if row is None:
            return None
        job = _job(row)
        if spec.barrier_plan_id is not None:
            exists = await connection.fetchval(
                "SELECT 1 FROM ingest.plan WHERE plan_id = $1", spec.barrier_plan_id
            )
            if exists is None:
                raise IntegrityError(f"Barrier plan {spec.barrier_plan_id} is not registered")
            await connection.execute(_INSERT_BARRIER, spec.barrier_plan_id, job.job_id)
        else:
            await self._event(connection, job, available_at=job.not_before, now=now)
        return job.job_id

    async def _register_plan(
        self, connection: PgConnection, plan: PlanRecord, *, now: datetime
    ) -> None:
        await connection.execute(
            _INSERT_PLAN,
            plan.plan_id,
            plan.content_hash,
            plan.snapshot_id,
            plan.policy_version,
            plan.family,
            plan.preflight_sha256,
            plan.page_count,
            len(plan.units),
            now,
        )
        for unit in plan.units:
            await connection.execute(
                _INSERT_UNIT,
                unit.unit_id,
                plan.plan_id,
                unit.ordinal,
                unit.kind,
                unit.route,
                list(unit.pages),
                unit.locator,
                unit.signals,
                unit.disposition.value if unit.disposition else None,
                unit.reason,
                now if unit.disposition else None,
                now,
            )

    async def _release(self, connection: PgConnection, plan_id: str, *, now: datetime) -> int:
        # The plan row lock serializes concurrent last-unit completions; the count below is a
        # new statement, so it sees every settlement committed before the lock was granted.
        if await connection.fetchval(
            "SELECT plan_id FROM ingest.plan WHERE plan_id = $1 FOR UPDATE", plan_id
        ) is None:
            return 0
        open_units = await connection.fetchval(
            "SELECT count(*) FROM ingest.unit WHERE plan_id = $1 AND disposition IS NULL",
            plan_id,
        )
        if open_units:
            return 0
        rows = await connection.fetch(_RELEASE_BARRIER, plan_id, now)
        for row in rows:
            await self._event(connection, _job(row), available_at=now, now=now)
        return len(rows)

    async def _settle(
        self,
        connection: PgConnection,
        job: StageJob,
        settled: tuple[UnitDisposition, str | None, ArtifactRef | None],
        *,
        now: datetime,
    ) -> str | None:
        disposition, reason, evidence = settled
        plan_id: str | None = await connection.fetchval(
            _SETTLE_UNIT,
            job.unit_id,
            disposition.value,
            reason,
            _dump(evidence),
            job.job_id,
            now,
        )
        return plan_id

    async def enqueue(self, specs: Sequence[JobSpec], *, now: datetime) -> list[str]:
        inserted: list[str] = []
        async with self._pool.acquire() as connection, connection.transaction():
            for spec in specs:
                job_id = await self._insert(connection, spec, now=now, parent_job_id=None)
                if job_id is not None:
                    inserted.append(job_id)
        return inserted

    async def get(self, job_id: str) -> StageJob | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(_SELECT_JOB, job_id)
        return None if row is None else _job(row)

    async def attempts(self, job_id: str) -> list[AttemptRecord]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT run_id, job_id, attempt, fencing_token, worker, started_at, finished_at,"
                " outcome, error_category, error, operation, usage"
                " FROM ingest.stage_attempt WHERE job_id = $1 ORDER BY attempt",
                job_id,
            )
        return [AttemptRecord.model_validate(dict(row)) for row in rows]

    async def claim(
        self, job_id: str, *, owner: str, lease_seconds: float, now: datetime
    ) -> Lease | None:
        run_id = new_run_id()
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _CLAIM,
                job_id,
                _CLAIMABLE,
                now,
                owner,
                now + timedelta(seconds=lease_seconds),
                run_id,
            )
            if row is None:
                return None
            job = _job(row)
            await connection.execute(
                _INSERT_ATTEMPT, run_id, job_id, job.attempt, job.fencing_token, owner, now
            )
        return Lease(job=job, run_id=run_id)

    async def heartbeat(self, lease: Lease, *, lease_seconds: float, now: datetime) -> Lease:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                _HEARTBEAT,
                lease.job.job_id,
                lease.fencing_token,
                now + timedelta(seconds=lease_seconds),
                now,
                _ACTIVE,
            )
        if row is None:
            raise LeaseLostError(f"Lease lost for job {lease.job.job_id}")
        return Lease(job=_job(row), run_id=lease.run_id)

    async def complete(self, lease: Lease, outcome: StageOutcome, *, now: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _COMPLETE,
                lease.job.job_id,
                lease.fencing_token,
                outcome.status.value,
                outcome.not_before,
                outcome.checkpoint,
                1 if outcome.counts_as_failure else 0,
                outcome.error_category.value if outcome.error_category else None,
                outcome.reason,
                now,
                _ACTIVE,
            )
            if row is None:
                raise LeaseLostError(f"Lease lost for job {lease.job.job_id}")
            job = _job(row)
            await connection.execute(
                _FINISH_ATTEMPT,
                lease.run_id,
                now,
                outcome.status.value,
                outcome.error_category.value if outcome.error_category else None,
                outcome.reason,
                outcome.retry_after_seconds,
                _dump(outcome.operation),
                _dump(outcome.usage),
            )
            for link in outcome.artifacts:
                artifact = link.artifact
                await connection.execute(
                    _INSERT_ARTIFACT,
                    artifact.sha256,
                    artifact.uri,
                    artifact.size_bytes,
                    artifact.media_type,
                    artifact.schema_name,
                    lease.run_id,
                    now,
                )
                stored_size = await connection.fetchval(
                    "SELECT size_bytes FROM ingest.artifact WHERE sha256 = $1", artifact.sha256
                )
                if stored_size != artifact.size_bytes:
                    raise IntegrityError(f"Artifact {artifact.sha256} size conflict")
                await connection.execute(
                    _LINK_ARTIFACT, job.job_id, link.role, artifact.sha256, lease.run_id
                )
            affected: set[str] = set()
            if outcome.plan is not None:
                await self._register_plan(connection, outcome.plan, now=now)
                affected.add(outcome.plan.plan_id)
            settled = outcome.settles_unit()
            if settled is not None:
                plan_id = await self._settle(connection, job, settled, now=now)
                if plan_id is not None:
                    affected.add(plan_id)
            for spec in outcome.next_jobs:
                await self._insert(connection, spec, now=now, parent_job_id=job.job_id)
                if spec.barrier_plan_id is not None:
                    affected.add(spec.barrier_plan_id)
            for plan_id in sorted(affected):
                await self._release(connection, plan_id, now=now)
            if outcome.status in RESCHEDULED_STATUSES and outcome.not_before is not None:
                await self._event(connection, job, available_at=outcome.not_before, now=now)
            freed = freed_submit_stage(job.stage, outcome)
            if freed is not None:
                await self._wake(connection, freed, 1, now=now)

    async def _wake(
        self, connection: PgConnection, submit: StageName, count: int, *, now: datetime
    ) -> int:
        if count <= 0:
            return 0
        rows = await connection.fetch(_WAKE_PARKED, submit.value, now, count)
        for row in rows:
            await self._event(connection, _job(row), available_at=now, now=now)
        return len(rows)

    async def wake_parked(self, submit: StageName, *, limit: int, now: datetime) -> int:
        async with self._pool.acquire() as connection, connection.transaction():
            in_flight = await connection.fetchval(
                _REMOTE_IN_FLIGHT_ALL, REMOTE_POLLS[submit].value, _IN_PROGRESS, submit.value
            )
            due = await connection.fetchval(_PARKED_DUE, submit.value, now)
            return await self._wake(
                connection, submit, limit - int(in_flight) - int(due), now=now
            )

    async def plan(self, plan_id: str) -> PlanRecord | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT plan_id, content_hash, snapshot_id, policy_version, family,"
                " preflight_sha256, page_count FROM ingest.plan WHERE plan_id = $1",
                plan_id,
            )
            if row is None:
                return None
            units = [_unit_state(unit) for unit in await connection.fetch(_SELECT_UNITS, plan_id)]
        return PlanRecord(**dict(row), units=tuple(state.unit for state in units))

    async def plan_units(self, plan_id: str) -> list[UnitState]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(_SELECT_UNITS, plan_id)
        return [_unit_state(row) for row in rows]

    async def replay(self, job_id: str, *, actor: str, reason: str, now: datetime) -> StageJob:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(f"{_SELECT_JOB} FOR UPDATE", job_id)
            if row is None:
                raise InvalidInputError(f"Unknown job {job_id}")
            current = _job(row)
            target = REPLAY_TARGETS.get(current.status)
            if target is None:
                raise InvalidInputError(f"Job {job_id} is {current.status.value}; not replayable")
            failed = current.status is JobStatus.FAILED_PERMANENT
            if failed and await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM ingest.unit WHERE unit_id = $1)", current.unit_id
            ):
                raise InvalidInputError(settled_unit_message(job_id, current.unit_id))
            require_transition(current.status, target)
            updated = await connection.fetchrow(
                _REPLAY,
                job_id,
                target.value,
                now,
                truncate_error(f"replayed by {actor}: {reason}"),
                failed,
            )
            job = _job(updated)
            await connection.execute(
                _INSERT_REPLAY, job_id, current.status.value, target.value, actor, reason, now
            )
            await self._event(connection, job, available_at=now, now=now)
        return job

    async def active_work(self, stage: StageName) -> ActiveWork:
        return await self._work(stage, "")

    async def account_work(self, stage: StageName, account: str | None) -> ActiveWork:
        if account is None:
            return await self._work(stage, " AND NOT inputs ? 'account'")
        return await self._work(stage, " AND inputs->>'account' = $4", account)

    async def _work(self, stage: StageName, condition: str, *args: Any) -> ActiveWork:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT count(*) AS jobs,"
                " COALESCE(sum(COALESCE((inputs->>'billable_pages')::bigint, 0)), 0) AS pages,"
                " count(*) FILTER (WHERE status = ANY($3::text[])) AS leased,"
                " count(*) FILTER (WHERE status = 'running') AS running"
                " FROM ingest.stage_job WHERE stage = $1 AND status = ANY($2::text[])"
                + condition,
                stage.value,
                _IN_PROGRESS,
                _ACTIVE,
                *args,
            )
        return ActiveWork(
            jobs=int(row["jobs"]),
            billable_pages=int(row["pages"]),
            leased=int(row["leased"]),
            running=int(row["running"]),
        )

    async def start_remote(
        self,
        lease: Lease,
        *,
        polls: StageName,
        limit: int,
        account: str | None = None,
        now: datetime,
    ) -> bool:
        job = lease.job
        async with self._pool.acquire() as connection, connection.transaction():
            # Serializes admission per submit stage and account; released at commit.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"tizert-remote:{job.stage.value}:{account or ''}",
            )
            in_flight = await connection.fetchval(
                _REMOTE_IN_FLIGHT, polls.value, _IN_PROGRESS, job.stage.value, job.job_id, account
            )
            if int(in_flight) >= limit:
                return False
            row = await connection.fetchrow(
                _START_REMOTE, job.job_id, lease.fencing_token, now, account
            )
        if row is None:
            raise LeaseLostError(f"Lease lost for job {job.job_id}")
        return True

    async def account_spend(self, account: str | None, providers: Sequence[str]) -> float:
        condition = "usage->>'account' IS NULL" if account is None else "usage->>'account' = $2"
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT COALESCE(sum((usage->>'cost_usd')::float8), 0) FROM ingest.stage_attempt"
                " WHERE usage IS NOT NULL AND usage->>'provider' = ANY($1::text[])"
                f" AND {condition}",
                list(providers),
                *(() if account is None else (account,)),
            )
        return float(value)

    async def provider_pages(self, provider: str) -> int:
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT COALESCE(sum((usage->>'pages')::bigint), 0) FROM ingest.stage_attempt"
                " WHERE usage IS NOT NULL AND usage->>'provider' = $1",
                provider,
            )
        return int(value)

    async def provider_usage(self, provider: str) -> ProviderUsage:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT COALESCE(sum((usage->>'pages')::bigint), 0) AS pages,"
                " COALESCE(sum((usage->>'input_tokens')::bigint), 0) AS input_tokens,"
                " COALESCE(sum((usage->>'output_tokens')::bigint), 0) AS output_tokens,"
                " COALESCE(sum((usage->>'requests')::bigint), 0) AS requests"
                " FROM ingest.stage_attempt WHERE usage IS NOT NULL AND usage->>'provider' = $1",
                provider,
            )
        return ProviderUsage(
            provider=provider,
            pages=int(row["pages"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            requests=int(row["requests"]),
        )

    async def release_ready_barriers(self, *, now: datetime, limit: int) -> int:
        async with self._pool.acquire() as connection:
            plan_ids = [row["plan_id"] for row in await connection.fetch(_READY_PLANS, limit)]
            released = 0
            for plan_id in plan_ids:
                async with connection.transaction():
                    released += await self._release(connection, plan_id, now=now)
        return released

    async def recover_expired(self, *, now: datetime, limit: int) -> RecoveryResult:
        requeued = failed = 0
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(_RECOVER, now, limit, _ACTIVE)
            for row in rows:
                job = _job(row)
                await connection.execute(
                    _EXPIRE_ATTEMPT, row["expired_run_id"], now, job.status.value
                )
                if job.status is JobStatus.RETRY_WAIT:
                    await self._event(connection, job, available_at=now, now=now)
                    requeued += 1
                else:
                    failed += 1
                    plan_id = await self._settle(
                        connection,
                        job,
                        (UnitDisposition.FAILED, "lease expired too often", None),
                        now=now,
                    )
                    if plan_id is not None:
                        await self._release(connection, plan_id, now=now)
        return RecoveryResult(requeued=requeued, failed=failed)

    async def redispatch_stale(self, *, now: datetime, stale_before: datetime, limit: int) -> int:
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(_STALE, stale_before, limit, _CLAIMABLE)
            for row in rows:
                await self._event(connection, _job(row), available_at=now, now=now)
        return len(rows)

    async def lease_batch(
        self, *, now: datetime, limit: int, claim_seconds: float
    ) -> list[OutboxEvent]:
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                _LEASE_EVENTS, now, limit, now + timedelta(seconds=claim_seconds)
            )
        return [
            OutboxEvent(
                event_id=row["event_id"],
                job_id=row["job_id"],
                pool=WorkerPool(row["pool"]),
                envelope=WorkEnvelope.model_validate(row["payload"]),
                available_at=row["available_at"],
            )
            for row in rows
        ]

    async def mark_sent(self, event_ids: Sequence[str], *, now: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE ingest.outbox SET sent_at = $2, claimed_until = NULL"
                " WHERE event_id = ANY($1::text[])",
                list(event_ids),
                now,
            )

    async def release(self, event_ids: Sequence[str], *, error: str, retry_at: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE ingest.outbox SET claimed_until = NULL, available_at = $2, last_error = $3"
                " WHERE event_id = ANY($1::text[])",
                list(event_ids),
                retry_at,
                error[:2000],
            )
