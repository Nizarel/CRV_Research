-- CRV retrieval projection evidence (read-only): one tab-separated line per chunk, in byte order
-- of projection_id, to locate any row the manifest digest flags. No output without the projection.
--   psql -X -At -v ON_ERROR_STOP=1 -f chunks.sql > chunks.tsv
-- Columns: projection_id, release_id, payload_sha256, md5(embedding_te3), md5(embedding_m3).
\set ON_ERROR_STOP on
\set QUIET on
SET statement_timeout = 0;
SET search_path = pg_catalog;
SELECT to_regclass('crv_retrieval.chunk') IS NOT NULL AS has_chunks \gset
\if :has_chunks
COPY (
    SELECT c.projection_id, c.release_id, c.payload_sha256,
        md5(c.embedding_te3::text), md5(c.embedding_m3::text)
    FROM crv_retrieval.chunk AS c
    ORDER BY c.projection_id COLLATE "C"
) TO STDOUT;
\endif
