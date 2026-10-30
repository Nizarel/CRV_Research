-- CRV migration inventory (read-only): one JSON object per line, for the export evidence and to
-- prepare the DGX staging target (placeholder roles, database settings and grants).
--   psql -X -At -v ON_ERROR_STOP=1 [-v release=crv-v1] -f inventory.sql
-- Uses PostgreSQL 16 catalogs (datlocprovider, daticulocale, membership inherit/set options).
\set ON_ERROR_STOP on
\set QUIET on
\if :{?release}
\else
\set release crv-v1
\endif
SET statement_timeout = '300s';
SET lock_timeout = '10s';
SET TimeZone = 'UTC';
SET search_path = pg_catalog;
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT json_build_object('kind', 'server', 'database', current_database(), 'user', current_user,
    'version', version(), 'version_num', current_setting('server_version_num')::integer,
    'checked_at', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'));

-- reset_val: the value a new session gets, ignoring the SET commands above.
SELECT json_build_object('kind', 'setting', 'name', s.name, 'value', s.reset_val,
    'unit', s.unit, 'source', s.source)
FROM pg_settings AS s
WHERE s.name IN ('shared_preload_libraries', 'default_toast_compression', 'jit',
        'random_page_cost', 'seq_page_cost', 'effective_cache_size', 'effective_io_concurrency',
        'work_mem', 'maintenance_work_mem', 'shared_buffers', 'max_connections',
        'max_parallel_workers_per_gather', 'max_parallel_maintenance_workers',
        'default_statistics_target', 'default_text_search_config', 'TimeZone', 'DateStyle',
        'lc_messages', 'lc_monetary', 'lc_numeric', 'lc_time', 'data_checksums',
        'server_encoding', 'password_encryption', 'search_path', 'row_security')
    OR s.name LIKE 'enable\_%' OR s.name LIKE 'hnsw.%' OR s.name LIKE 'age.%'
ORDER BY s.name;

SELECT json_build_object('kind', 'database', 'name', d.datname,
    'owner', pg_get_userbyid(d.datdba), 'encoding', pg_encoding_to_char(d.encoding),
    'locale_provider', d.datlocprovider, 'collate', d.datcollate, 'ctype', d.datctype,
    'icu_locale', d.daticulocale, 'icu_rules', d.daticurules,
    'collation_version', d.datcollversion, 'connection_limit', d.datconnlimit,
    'acl', d.datacl::text[], 'bytes', pg_database_size(d.oid))
FROM pg_database AS d
WHERE d.datname = current_database();

-- Database-wide (role null) and per-role settings in this database; pg_dump omits both.
SELECT json_build_object('kind', 'db_setting', 'role', r.rolname, 'settings', s.setconfig)
FROM pg_db_role_setting AS s
LEFT JOIN pg_roles AS r ON r.oid = s.setrole
WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY r.rolname NULLS FIRST;

SELECT json_build_object('kind', 'extension', 'name', e.extname, 'version', e.extversion,
    'schema', e.extnamespace::regnamespace::text, 'owner', pg_get_userbyid(e.extowner))
FROM pg_extension AS e
ORDER BY e.extname;

SELECT json_build_object('kind', 'available_extension', 'name', a.name,
    'default_version', a.default_version, 'installed_version', a.installed_version)
FROM pg_available_extensions AS a
WHERE a.name IN ('age', 'vector', 'pg_trgm', 'unaccent', 'pg_stat_statements', 'plpgsql',
    'pg_cron', 'pgaadauth', 'azure', 'pg_duckdb')
ORDER BY a.name;

SELECT json_build_object('kind', 'schema', 'name', n.nspname,
    'owner', pg_get_userbyid(n.nspowner), 'acl', n.nspacl::text[],
    'extension', (SELECT e.extname FROM pg_depend AS d
        JOIN pg_extension AS e ON e.oid = d.refobjid
        WHERE d.classid = 'pg_namespace'::regclass AND d.objid = n.oid AND d.deptype = 'e'))
FROM pg_namespace AS n
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY n.nspname;

SELECT json_build_object('kind', 'relation', 'name', c.oid::regclass::text, 'relkind', c.relkind,
    'owner', pg_get_userbyid(c.relowner), 'estimated_rows', c.reltuples::bigint,
    'total_bytes', pg_total_relation_size(c.oid), 'row_security', c.relrowsecurity)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY c.oid::regclass::text;

-- Roles that own or are granted anything a dump of this database restores, plus both sides of
-- their memberships. The target recreates them as NOLOGIN placeholders (no credentials).
WITH user_namespace AS (
    SELECT n.oid FROM pg_namespace AS n
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_namespace'::regclass
            AND d.objid = n.oid AND d.deptype = 'e')
), objects AS (
    SELECT c.relowner AS owner, c.relacl AS acl FROM pg_class AS c
    WHERE c.relnamespace IN (SELECT oid FROM user_namespace)
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_class'::regclass
            AND d.objid = c.oid AND d.deptype = 'e')
    UNION ALL
    SELECT c.relowner, a.attacl FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid
    WHERE a.attacl IS NOT NULL AND c.relnamespace IN (SELECT oid FROM user_namespace)
    UNION ALL
    SELECT p.proowner, p.proacl FROM pg_proc AS p
    WHERE p.pronamespace IN (SELECT oid FROM user_namespace)
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass
            AND d.objid = p.oid AND d.deptype = 'e')
    UNION ALL
    SELECT n.nspowner, n.nspacl FROM pg_namespace AS n
    WHERE n.oid IN (SELECT oid FROM user_namespace)
    UNION ALL
    SELECT t.typowner, t.typacl FROM pg_type AS t
    WHERE t.typnamespace IN (SELECT oid FROM user_namespace) AND t.typtype IN ('c', 'd', 'e', 'r')
        AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_type'::regclass
            AND d.objid = t.oid AND d.deptype = 'e')
    UNION ALL
    SELECT cfg.cfgowner, NULL FROM pg_ts_config AS cfg
    WHERE cfg.cfgnamespace IN (SELECT oid FROM user_namespace)
    UNION ALL
    SELECT dict.dictowner, NULL FROM pg_ts_dict AS dict
    WHERE dict.dictnamespace IN (SELECT oid FROM user_namespace)
    UNION ALL
    SELECT da.defaclrole, da.defaclacl FROM pg_default_acl AS da
    UNION ALL
    SELECT db.datdba, db.datacl FROM pg_database AS db WHERE db.datname = current_database()
    UNION ALL
    SELECT lo.lomowner, lo.lomacl FROM pg_largeobject_metadata AS lo
    UNION ALL
    SELECT et.evtowner, NULL FROM pg_event_trigger AS et
    UNION ALL
    SELECT pub.pubowner, NULL FROM pg_publication AS pub
), referenced AS (
    SELECT o.owner AS oid FROM objects AS o
    UNION
    SELECT a.grantee FROM objects AS o, aclexplode(o.acl) AS a WHERE o.acl IS NOT NULL
    UNION
    SELECT a.grantor FROM objects AS o, aclexplode(o.acl) AS a WHERE o.acl IS NOT NULL
), membership AS (
    SELECT m.* FROM pg_auth_members AS m
    WHERE m.roleid IN (SELECT oid FROM referenced) OR m.member IN (SELECT oid FROM referenced)
), involved AS (
    SELECT oid FROM referenced
    UNION SELECT roleid FROM membership
    UNION SELECT member FROM membership
)
SELECT line FROM (
    SELECT 1 AS part, r.rolname::text AS sort1, ''::text AS sort2,
        json_build_object('kind', 'role', 'name', r.rolname, 'login', r.rolcanlogin,
            'inherit', r.rolinherit, 'superuser', r.rolsuper, 'createrole', r.rolcreaterole,
            'createdb', r.rolcreatedb, 'bypassrls', r.rolbypassrls,
            'predefined', r.rolname ~ '^pg_')::text AS line
    FROM pg_roles AS r WHERE r.oid IN (SELECT oid FROM involved)
    UNION ALL
    SELECT 2, pg_get_userbyid(m.roleid)::text, pg_get_userbyid(m.member)::text,
        json_build_object('kind', 'membership', 'role', pg_get_userbyid(m.roleid),
            'member', pg_get_userbyid(m.member), 'admin_option', m.admin_option,
            'inherit_option', m.inherit_option, 'set_option', m.set_option)::text
    FROM membership AS m
) AS roles
ORDER BY part, sort1, sort2;

-- pg_dump needs SELECT on every table and sequence and USAGE on every schema it exports.
SELECT line FROM (
    SELECT c.oid::regclass::text AS sort_key,
        json_build_object('kind', 'unreadable', 'name', c.oid::regclass::text,
            'relkind', c.relkind)::text AS line
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'm', 'S') AND n.nspname !~ '^pg_'
        AND n.nspname <> 'information_schema' AND NOT has_table_privilege(c.oid, 'SELECT')
    UNION ALL
    SELECT n.nspname::text, json_build_object('kind', 'unreadable', 'name', n.nspname,
        'relkind', 'schema')::text
    FROM pg_namespace AS n
    WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        AND NOT has_schema_privilege(n.oid, 'USAGE')
) AS gaps
ORDER BY sort_key;

SELECT json_build_object('kind', 'row_security', 'name', c.oid::regclass::text,
    'forced', c.relforcerowsecurity)
FROM pg_class AS c
WHERE c.relrowsecurity
ORDER BY c.oid::regclass::text;

SELECT json_build_object('kind', 'special_objects',
    'large_objects', (SELECT count(*) FROM pg_largeobject_metadata),
    'publications', (SELECT count(*) FROM pg_publication),
    'event_triggers', (SELECT count(*) FROM pg_event_trigger));

SELECT json_build_object('kind', 'invalid_index', 'name', i.indexrelid::regclass::text,
    'valid', i.indisvalid, 'ready', i.indisready)
FROM pg_index AS i
WHERE NOT (i.indisvalid AND i.indisready)
ORDER BY i.indexrelid::regclass::text;

SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age') AS has_age \gset
\if :has_age
SELECT json_build_object('kind', 'age_graph', 'name', g.name, 'namespace', g.namespace::text,
    'graphid_matches_namespace', g.graphid = g.namespace::oid)
FROM ag_catalog.ag_graph AS g
ORDER BY g.name;

SELECT json_build_object('kind', 'age_label', 'graph', g.name, 'label', l.name,
    'label_kind', l.kind, 'relation', l.relation::text,
    'rows', (xpath('/row/n/text()', query_to_xml(
        format('SELECT count(*) AS n FROM %s', l.relation), false, true, '')))[1]::text::bigint)
FROM ag_catalog.ag_label AS l
JOIN ag_catalog.ag_graph AS g ON g.graphid = l.graph
ORDER BY g.name, l.name;
\endif

SELECT to_regclass('ingest.schema_migration') IS NOT NULL AS has_ledger \gset
\if :has_ledger
SELECT json_build_object('kind', 'migration', 'version', m.version, 'sha256', m.sha256,
    'applied_at', m.applied_at, 'applied_by', m.applied_by)
FROM ingest.schema_migration AS m
ORDER BY m.version;
\endif

SELECT to_regprocedure('crv_retrieval.release_status(text)') IS NOT NULL AS has_retrieval \gset
\if :has_retrieval
SELECT json_build_object('kind', 'release_status', 'release', :'release', 'status', row_to_json(s))
FROM crv_retrieval.release_status(:'release') AS s;
\endif

SELECT json_build_object('kind', 'session', 'user', a.usename, 'application', a.application_name,
    'state', a.state, 'backend_type', a.backend_type,
    'transaction_seconds', round(extract(epoch FROM now() - a.xact_start)))
FROM pg_stat_activity AS a
WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
ORDER BY a.usename, a.application_name, a.state;

COMMIT;
