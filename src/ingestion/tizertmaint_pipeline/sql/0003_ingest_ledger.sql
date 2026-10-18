-- 0003_ingest_ledger: durable stage ledger, attempts, artifacts, source snapshots and outbox.
-- Additive only. Requires 0002_knowledge_prerequisites (ingest schema + migration registry).

DO $migration$
BEGIN
    IF to_regclass('ingest.schema_migration') IS NULL THEN
        RAISE EXCEPTION '0002_knowledge_prerequisites must be applied before 0003';
    END IF;
END
$migration$;

CREATE TABLE ingest.source_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL,
    placement_id TEXT NOT NULL,
    container_name TEXT NOT NULL,
    blob_name TEXT NOT NULL,
    etag TEXT NOT NULL CHECK (etag <> ''),
    version_id TEXT,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    content_type TEXT,
    last_modified TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_object_id, etag, content_hash)
);

CREATE INDEX source_snapshot_content_idx ON ingest.source_snapshot (content_hash);
CREATE INDEX source_snapshot_source_idx ON ingest.source_snapshot (source_object_id, observed_at DESC);

CREATE TABLE ingest.stage_job (
    job_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    pool TEXT NOT NULL CHECK (pool IN ('io', 'cpu', 'heavy', 'publish')),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'leased', 'running', 'awaiting_remote', 'collecting', 'retry_wait',
        'succeeded', 'needs_review', 'no_evidence', 'unsupported', 'quarantined',
        'failed_permanent', 'superseded', 'cancelled', 'submission_unknown'
    )),
    priority INTEGER NOT NULL DEFAULT 100,
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_snapshot_id TEXT,
    content_hash TEXT CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    parent_job_id TEXT REFERENCES ingest.stage_job (job_id),
    not_before TIMESTAMPTZ NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    max_failures INTEGER NOT NULL CHECK (max_failures > 0),
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    current_run_id TEXT,
    last_error_category TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (unit_id, stage, fingerprint),
    CONSTRAINT stage_job_lease_consistent CHECK (
        (status IN ('leased', 'running', 'collecting'))
        = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND current_run_id IS NOT NULL)
    )
);

CREATE INDEX stage_job_due_idx ON ingest.stage_job (not_before)
    WHERE status IN ('pending', 'retry_wait', 'awaiting_remote');
CREATE INDEX stage_job_lease_idx ON ingest.stage_job (lease_expires_at)
    WHERE status IN ('leased', 'running', 'collecting');
CREATE INDEX stage_job_parent_idx ON ingest.stage_job (parent_job_id) WHERE parent_job_id IS NOT NULL;
CREATE INDEX stage_job_snapshot_idx ON ingest.stage_job (source_snapshot_id)
    WHERE source_snapshot_id IS NOT NULL;

CREATE TABLE ingest.stage_attempt (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES ingest.stage_job (job_id),
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    fencing_token BIGINT NOT NULL,
    worker TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    outcome TEXT,
    error_category TEXT,
    error TEXT,
    retry_after_seconds DOUBLE PRECISION,
    operation JSONB,
    usage JSONB,
    UNIQUE (job_id, attempt)
);

CREATE TABLE ingest.artifact (
    sha256 TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    uri TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    media_type TEXT NOT NULL,
    schema_name TEXT,
    producer_run_id TEXT NOT NULL REFERENCES ingest.stage_attempt (run_id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE ingest.job_artifact (
    job_id TEXT NOT NULL REFERENCES ingest.stage_job (job_id),
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES ingest.artifact (sha256),
    run_id TEXT NOT NULL REFERENCES ingest.stage_attempt (run_id),
    PRIMARY KEY (job_id, role, sha256)
);

CREATE TABLE ingest.outbox (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES ingest.stage_job (job_id),
    pool TEXT NOT NULL CHECK (pool IN ('io', 'cpu', 'heavy', 'publish')),
    payload JSONB NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    claimed_until TIMESTAMPTZ,
    send_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX outbox_pending_idx ON ingest.outbox (available_at) WHERE sent_at IS NULL;
CREATE INDEX outbox_job_idx ON ingest.outbox (job_id, sent_at);

-- Runtime role: no DELETE anywhere, history is append-only or state-updated in place.
-- Entra principals are mapped and granted this role by an administrator, not here.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tizert_ingest_worker') THEN
        CREATE ROLE tizert_ingest_worker NOLOGIN;
    END IF;
END
$roles$;

GRANT USAGE ON SCHEMA ingest TO tizert_ingest_worker;
GRANT SELECT ON ingest.schema_migration TO tizert_ingest_worker;
GRANT SELECT, INSERT, UPDATE ON ingest.stage_job, ingest.stage_attempt, ingest.outbox
    TO tizert_ingest_worker;
GRANT SELECT, INSERT ON ingest.source_snapshot, ingest.artifact, ingest.job_artifact
    TO tizert_ingest_worker;
