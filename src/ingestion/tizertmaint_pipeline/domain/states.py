"""Stage names, worker pools, job statuses and the allowed status transitions."""

from __future__ import annotations

from enum import StrEnum


class StageName(StrEnum):
    SNAPSHOT = "snapshot"
    PREFLIGHT = "preflight"
    PLAN = "plan"
    NATIVE_PARSE = "native_parse"
    RENDER = "render"
    DI_SUBMIT = "di_submit"
    DI_POLL = "di_poll"
    CU_SUBMIT = "cu_submit"
    CU_POLL = "cu_poll"
    FIGURE_ASSETS = "figure_assets"
    NORMALIZE = "normalize"
    CHUNK = "chunk"
    EMBED = "embed"
    CANDIDATES = "candidates"
    VALIDATE = "validate"
    PROJECT_SEARCH = "project_search"
    RELEASE_BUILD = "release_build"
    RELEASE_PUBLISH = "release_publish"
    RELEASE_ACTIVATE = "release_activate"


class WorkerPool(StrEnum):
    """Workload class; each pool has its own queue, scaling rule and resource limits."""

    IO = "io"
    CPU = "cpu"
    HEAVY = "heavy"
    PUBLISH = "publish"
    POLL = "poll"
    """Remote-operation polls: kept off the io queue so a submit or projection burst cannot
    delay them while their operations hold provider slots."""


DEFAULT_STAGE_POOLS: dict[StageName, WorkerPool] = {
    StageName.SNAPSHOT: WorkerPool.IO,
    StageName.PREFLIGHT: WorkerPool.CPU,
    StageName.PLAN: WorkerPool.CPU,
    StageName.NATIVE_PARSE: WorkerPool.CPU,
    StageName.RENDER: WorkerPool.HEAVY,
    StageName.DI_SUBMIT: WorkerPool.IO,
    StageName.DI_POLL: WorkerPool.POLL,
    StageName.CU_SUBMIT: WorkerPool.IO,
    StageName.CU_POLL: WorkerPool.POLL,
    StageName.FIGURE_ASSETS: WorkerPool.IO,
    StageName.NORMALIZE: WorkerPool.CPU,
    StageName.CHUNK: WorkerPool.CPU,
    StageName.EMBED: WorkerPool.IO,
    StageName.CANDIDATES: WorkerPool.CPU,
    StageName.VALIDATE: WorkerPool.CPU,
    StageName.PROJECT_SEARCH: WorkerPool.IO,
    StageName.RELEASE_BUILD: WorkerPool.PUBLISH,
    StageName.RELEASE_PUBLISH: WorkerPool.PUBLISH,
    StageName.RELEASE_ACTIVATE: WorkerPool.PUBLISH,
}

REMOTE_POLLS: dict[StageName, StageName] = {
    StageName.CU_SUBMIT: StageName.CU_POLL,
    StageName.DI_SUBMIT: StageName.DI_POLL,
}
"""Submit stage -> poll stage whose unfinished jobs hold that submit's remote-operation slots."""


class JobStatus(StrEnum):
    BLOCKED = "blocked"
    """Waits for a plan barrier; the ledger releases it to ``pending`` when all units settle."""
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    AWAITING_REMOTE = "awaiting_remote"
    COLLECTING = "collecting"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    NO_EVIDENCE = "no_evidence"
    UNSUPPORTED = "unsupported"
    QUARANTINED = "quarantined"
    FAILED_PERMANENT = "failed_permanent"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    SUBMISSION_UNKNOWN = "submission_unknown"


CLAIMABLE_STATUSES = frozenset({JobStatus.PENDING, JobStatus.RETRY_WAIT, JobStatus.AWAITING_REMOTE})
"""Statuses a worker may lease once ``not_before`` has passed."""

ACTIVE_STATUSES = frozenset({JobStatus.LEASED, JobStatus.RUNNING, JobStatus.COLLECTING})
"""Statuses that hold a lease and a fencing token."""

RESCHEDULED_STATUSES = frozenset({JobStatus.RETRY_WAIT, JobStatus.AWAITING_REMOTE})
"""Outcome statuses that need a future wake-up event."""

WORK_IN_PROGRESS = CLAIMABLE_STATUSES | ACTIVE_STATUSES
"""Jobs that may still run: queued, leased, waiting on a remote operation or retrying."""

TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.NEEDS_REVIEW,
        JobStatus.NO_EVIDENCE,
        JobStatus.UNSUPPORTED,
        JobStatus.QUARANTINED,
        JobStatus.FAILED_PERMANENT,
        JobStatus.SUPERSEDED,
        JobStatus.CANCELLED,
    }
)

OUTCOME_STATUSES = (TERMINAL_STATUSES | RESCHEDULED_STATUSES | {JobStatus.SUBMISSION_UNKNOWN}) - {
    JobStatus.CANCELLED
}
"""Statuses a stage execution may report when it releases its lease."""

_WITHDRAWN = frozenset({JobStatus.CANCELLED, JobStatus.SUPERSEDED})

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.BLOCKED: frozenset({JobStatus.PENDING}) | _WITHDRAWN,
    JobStatus.PENDING: frozenset({JobStatus.LEASED}) | _WITHDRAWN,
    JobStatus.RETRY_WAIT: frozenset({JobStatus.LEASED}) | _WITHDRAWN,
    JobStatus.AWAITING_REMOTE: frozenset({JobStatus.LEASED}) | _WITHDRAWN,
    JobStatus.LEASED: frozenset({JobStatus.RUNNING, JobStatus.COLLECTING})
    | OUTCOME_STATUSES
    | _WITHDRAWN,
    JobStatus.RUNNING: frozenset({JobStatus.COLLECTING}) | OUTCOME_STATUSES | _WITHDRAWN,
    JobStatus.COLLECTING: OUTCOME_STATUSES | _WITHDRAWN,
    JobStatus.SUBMISSION_UNKNOWN: frozenset(
        {JobStatus.RETRY_WAIT, JobStatus.AWAITING_REMOTE, JobStatus.FAILED_PERMANENT}
    )
    | _WITHDRAWN,
    JobStatus.NEEDS_REVIEW: frozenset({JobStatus.PENDING}) | _WITHDRAWN,
    JobStatus.QUARANTINED: _WITHDRAWN,
    JobStatus.FAILED_PERMANENT: frozenset({JobStatus.PENDING, JobStatus.SUPERSEDED}),
    JobStatus.UNSUPPORTED: frozenset({JobStatus.SUPERSEDED}),
    JobStatus.NO_EVIDENCE: frozenset({JobStatus.SUPERSEDED}),
    JobStatus.SUCCEEDED: frozenset({JobStatus.SUPERSEDED}),
    JobStatus.SUPERSEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


REPLAY_TARGETS: dict[JobStatus, JobStatus] = {
    JobStatus.SUBMISSION_UNKNOWN: JobStatus.RETRY_WAIT,
    JobStatus.NEEDS_REVIEW: JobStatus.PENDING,
    JobStatus.FAILED_PERMANENT: JobStatus.PENDING,
}
"""Statuses an operator may replay, and the status the replayed job resumes in.

A ``failed_permanent`` job resumes with a fresh failure budget, unless it settled a plan unit:
that unit's plan has moved on, so the arm is re-run under a new policy instead."""


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def settled_unit_message(job_id: str, unit_id: str) -> str:
    return (
        f"Job {job_id} failed plan unit {unit_id}, which is settled;"
        " re-run the arm under a new policy instead"
    )


def require_transition(current: JobStatus, target: JobStatus) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Illegal job status transition {current.value} -> {target.value}")
