DO $migration$
BEGIN
    IF current_database() <> 'ingestion-catalog' THEN
        RAISE EXCEPTION 'Knowledge prerequisites require ingestion-catalog';
    END IF;
END
$migration$;

CREATE EXTENSION IF NOT EXISTS age CASCADE;

CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS acl;

REVOKE ALL ON SCHEMA ingest, kg, acl FROM PUBLIC;

CREATE TABLE IF NOT EXISTS ingest.schema_migration (
    version TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by TEXT NOT NULL DEFAULT current_user
);

SET LOCAL search_path = ag_catalog, pg_catalog;

DO $graph$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'tizert_pilot') THEN
        PERFORM ag_catalog.create_graph('tizert_pilot');
    END IF;
END
$graph$;