-- CRV export freeze check (read-only): cumulative write counters of every user table and the
-- position of every sequence, as sorted tab-separated lines. Two runs that differ mean a writer
-- was active between them. Pending statistics reach these views within seconds of each commit;
-- wait at least a minute between runs.
--   psql -X -At -v ON_ERROR_STOP=1 -f freeze-check.sql
\set ON_ERROR_STOP on
\set QUIET on
SET statement_timeout = '120s';
SET search_path = pg_catalog;

SELECT line FROM (
    SELECT format('table %s.%s', quote_ident(t.schemaname), quote_ident(t.relname)) AS sort_key,
        concat_ws(E'\t', 'table', quote_ident(t.schemaname) || '.' || quote_ident(t.relname),
            t.n_tup_ins, t.n_tup_upd, t.n_tup_del) AS line
    FROM pg_stat_user_tables AS t
    UNION ALL
    SELECT format('sequence %s.%s', quote_ident(s.schemaname), quote_ident(s.sequencename)),
        concat_ws(E'\t', 'sequence',
            quote_ident(s.schemaname) || '.' || quote_ident(s.sequencename),
            coalesce(s.last_value::text, 'unused'))
    FROM pg_sequences AS s
    WHERE s.schemaname !~ '^pg_'
) AS counters
ORDER BY sort_key;
