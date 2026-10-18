-- 0008_ingest_stage_status_idx: admission and status counts filter stage_job by stage and status.
-- Requires 0003. On a large live ledger create it CONCURRENTLY beforehand; this is then a no-op.

CREATE INDEX IF NOT EXISTS stage_job_stage_status_idx ON ingest.stage_job (stage, status);
