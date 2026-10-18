-- 0005_ingest_extraction_cache: paid analysis results reused across routing policies.
-- The fingerprint covers provider, API version, content, unit pages, request and analyzer
-- profile, so a hit is the same analysis by construction. Additive only. Requires 0004.

DO $migration$
BEGIN
    IF to_regclass('ingest.plan') IS NULL THEN
        RAISE EXCEPTION '0004_ingest_evidence must be applied before 0005';
    END IF;
END
$migration$;

CREATE TABLE ingest.extraction_cache (
    fingerprint TEXT PRIMARY KEY CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    provider TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    raw JSONB NOT NULL,
    page_map JSONB,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX extraction_cache_content_idx ON ingest.extraction_cache (content_hash, provider);

GRANT SELECT, INSERT ON ingest.extraction_cache TO tizert_ingest_worker;
