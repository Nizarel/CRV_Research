-- 0009_ingest_parked_submits: CU/DI submits parked on the in-flight limit are woken oldest first
-- when a remote slot frees. Requires 0003.

CREATE INDEX IF NOT EXISTS stage_job_parked_idx
    ON ingest.stage_job (stage, priority, created_at, job_id)
    WHERE status = 'retry_wait' AND last_error_category = 'slot_wait';
