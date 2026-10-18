-- 0010_ingest_poll_pool: CU and DI polls run on their own pool and queue (ingest-poll).
-- 0003 declared the pool checks inline (auto-named); jobs already queued keep their pool.

DO $pool$
DECLARE
    target TEXT;
    existing TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['ingest.stage_job', 'ingest.outbox'] LOOP
        FOR existing IN
            SELECT con.conname FROM pg_constraint AS con
            WHERE con.conrelid = target::regclass
              AND con.contype = 'c'
              AND pg_get_constraintdef(con.oid) LIKE '%publish%'
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', target, existing);
        END LOOP;
    END LOOP;
END
$pool$;

-- Existing rows passed the narrower check; NOT VALID skips a locked scan of the live outbox.
ALTER TABLE ingest.stage_job ADD CONSTRAINT stage_job_pool_valid
    CHECK (pool IN ('io', 'cpu', 'heavy', 'publish', 'poll')) NOT VALID;
ALTER TABLE ingest.outbox ADD CONSTRAINT outbox_pool_valid
    CHECK (pool IN ('io', 'cpu', 'heavy', 'publish', 'poll')) NOT VALID;
