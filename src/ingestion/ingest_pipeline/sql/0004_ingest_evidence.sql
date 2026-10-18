-- 0004_ingest_evidence: extraction plans and units, plan barriers (fan-in), evidence documents,
-- chunks, the embedding cache, figure assets, search projection items and legacy reconciliation.
-- Additive only. Requires 0003_ingest_ledger.

DO $migration$
BEGIN
    IF to_regclass('ingest.stage_job') IS NULL THEN
        RAISE EXCEPTION '0003_ingest_ledger must be applied before 0004';
    END IF;
END
$migration$;

-- 'blocked' jobs wait for a plan barrier. 0003 declared the status check inline (auto-named).
DO $status$
DECLARE
    existing TEXT;
BEGIN
    SELECT con.conname INTO existing
    FROM pg_constraint AS con
    WHERE con.conrelid = 'ingest.stage_job'::regclass
      AND con.contype = 'c'
      AND pg_get_constraintdef(con.oid) LIKE '%awaiting_remote%';
    IF existing IS NOT NULL THEN
        EXECUTE format('ALTER TABLE ingest.stage_job DROP CONSTRAINT %I', existing);
    END IF;
END
$status$;

ALTER TABLE ingest.stage_job ADD CONSTRAINT stage_job_status_valid CHECK (status IN (
    'blocked', 'pending', 'leased', 'running', 'awaiting_remote', 'collecting', 'retry_wait',
    'succeeded', 'needs_review', 'no_evidence', 'unsupported', 'quarantined',
    'failed_permanent', 'superseded', 'cancelled', 'submission_unknown'
));

CREATE INDEX source_snapshot_placement_idx
    ON ingest.source_snapshot (placement_id, observed_at DESC);

CREATE TABLE ingest.plan (
    plan_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    snapshot_id TEXT,
    policy_version TEXT NOT NULL,
    family TEXT NOT NULL,
    preflight_sha256 TEXT CHECK (preflight_sha256 ~ '^[0-9a-f]{64}$'),
    page_count INTEGER CHECK (page_count >= 0),
    unit_count INTEGER NOT NULL CHECK (unit_count >= 0),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX plan_content_idx ON ingest.plan (content_hash, policy_version);

CREATE TABLE ingest.unit (
    unit_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ingest.plan (plan_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL,
    route TEXT NOT NULL,
    pages INTEGER[] NOT NULL DEFAULT '{}',
    locator JSONB NOT NULL DEFAULT '{}'::jsonb,
    signals JSONB NOT NULL DEFAULT '{}'::jsonb,
    disposition TEXT CHECK (disposition IN (
        'evidence', 'no_evidence', 'failed', 'unsupported', 'quarantined', 'superseded',
        'cancelled'
    )),
    reason TEXT,
    evidence JSONB,
    disposed_by_job_id TEXT,
    disposed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (plan_id, ordinal),
    CONSTRAINT unit_evidence_consistent CHECK ((disposition = 'evidence') = (evidence IS NOT NULL))
);

CREATE INDEX unit_open_idx ON ingest.unit (plan_id) WHERE disposition IS NULL;

CREATE TABLE ingest.plan_barrier (
    plan_id TEXT NOT NULL REFERENCES ingest.plan (plan_id),
    job_id TEXT NOT NULL REFERENCES ingest.stage_job (job_id),
    released_at TIMESTAMPTZ,
    PRIMARY KEY (plan_id, job_id)
);

CREATE INDEX plan_barrier_pending_idx ON ingest.plan_barrier (plan_id) WHERE released_at IS NULL;

-- Artifact references are JSON copies of ArtifactRef: stages write these rows before the ledger
-- commits the artifact row, and every reader verifies digests on download anyway.
CREATE TABLE ingest.evidence_document (
    evidence_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES ingest.plan (plan_id),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    policy_version TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('complete', 'partial')),
    status TEXT NOT NULL CHECK (status IN ('chunked', 'embedded')),
    manifest JSONB NOT NULL,
    chunk_set JSONB NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX evidence_document_content_idx
    ON ingest.evidence_document (content_hash, policy_version, updated_at DESC);

CREATE TABLE ingest.chunk (
    chunk_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES ingest.evidence_document (evidence_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL,
    chunker TEXT NOT NULL,
    tokenizer TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    original_pages INTEGER[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (evidence_id, ordinal)
);

-- Exact-lookup embedding cache: float32 little-endian vectors; similarity search lives in Search.
CREATE TABLE ingest.embedding (
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    encoding TEXT NOT NULL DEFAULT 'f32le' CHECK (encoding = 'f32le'),
    vector BYTEA NOT NULL,
    vector_sha256 TEXT NOT NULL CHECK (vector_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (input_sha256, model, dimensions),
    CONSTRAINT embedding_length CHECK (octet_length(vector) = 4 * dimensions)
);

ALTER TABLE ingest.embedding ALTER COLUMN vector SET STORAGE EXTERNAL;

CREATE TABLE ingest.figure_asset (
    unit_id TEXT NOT NULL REFERENCES ingest.unit (unit_id),
    figure_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('stored', 'failed', 'expired')),
    artifact JSONB,
    reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (unit_id, figure_id),
    CONSTRAINT figure_asset_consistent CHECK ((state = 'stored') = (artifact IS NOT NULL))
);

CREATE TABLE ingest.projection_item (
    store TEXT NOT NULL,
    target TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    placement_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL CHECK (state IN (
        'pending', 'acknowledged', 'verified', 'retired', 'failed_permanent'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (store, target, projection_id)
);

CREATE INDEX projection_item_placement_idx
    ON ingest.projection_item (store, target, placement_id, state);

-- Operator replays of submission_unknown / needs_review jobs; append-only audit.
CREATE TABLE ingest.job_replay (
    replay_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES ingest.stage_job (job_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    replayed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE ingest.legacy_reconciliation (
    legacy_key TEXT PRIMARY KEY,
    content_hash TEXT CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    legacy_state TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'legacy_unknown', 'legacy_evidence_available', 'verified'
    )),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT,
    checked_at TIMESTAMPTZ NOT NULL
);

GRANT SELECT, INSERT, UPDATE ON
    ingest.plan, ingest.unit, ingest.plan_barrier, ingest.evidence_document,
    ingest.figure_asset, ingest.projection_item, ingest.legacy_reconciliation
    TO tizert_ingest_worker;
GRANT SELECT, INSERT ON ingest.chunk, ingest.embedding, ingest.job_replay TO tizert_ingest_worker;
