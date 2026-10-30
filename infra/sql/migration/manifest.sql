-- CRV migration manifest (read-only): a deterministic, OID-free description of one database for
-- the source/target comparison. One JSON object per line; diff the source and target outputs.
--   psql -X -At -v ON_ERROR_STOP=1 [-v catalog_only=1] -f manifest.sql
-- catalog_only skips table contents, sequence positions and the AGE graph catalog (schema-only
-- rehearsal). Objects that belong to an extension are compared through the extension version
-- only: their owner is whoever created the extension. A missing ACL prints as its default, which
-- is what pg_dump restores it as. The full run reads every row, so run it inside the export
-- window.
\set ON_ERROR_STOP on
\set QUIET on
\if :{?catalog_only}
\else
\set catalog_only 0
\endif
-- Non-system schemas that no extension owns, as an IN list.
\set user_namespaces '(SELECT n.oid FROM pg_namespace AS n WHERE n.nspname !~ ''^pg_'' AND n.nspname <> ''information_schema'' AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = ''pg_namespace''::regclass AND d.objid = n.oid AND d.deptype = ''e''))'
SET statement_timeout = 0;
SET lock_timeout = '10s';
SET TimeZone = 'UTC';
SET DateStyle = 'ISO, YMD';
SET IntervalStyle = 'postgres';
SET extra_float_digits = 1;
SET bytea_output = 'hex';
-- Names below print schema-qualified, except pg_catalog objects.
SET search_path = pg_catalog;
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT json_build_object('kind', 'database', 'owner', pg_get_userbyid(d.datdba),
    'encoding', pg_encoding_to_char(d.encoding), 'locale_provider', d.datlocprovider,
    'collate', d.datcollate, 'ctype', d.datctype, 'icu_locale', d.daticulocale,
    'acl', ARRAY(SELECT x::text FROM unnest(coalesce(d.datacl, acldefault('d', d.datdba))) AS x
        ORDER BY x::text COLLATE "C"))
FROM pg_database AS d
WHERE d.datname = current_database();

SELECT json_build_object('kind', 'db_setting', 'role', r.rolname,
    'settings', ARRAY(SELECT x FROM unnest(s.setconfig) AS x ORDER BY x COLLATE "C"))
FROM pg_db_role_setting AS s
LEFT JOIN pg_roles AS r ON r.oid = s.setrole
WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY r.rolname COLLATE "C" NULLS FIRST;

SELECT json_build_object('kind', 'extension', 'name', e.extname, 'version', e.extversion,
    'schema', e.extnamespace::regnamespace::text)
FROM pg_extension AS e
ORDER BY e.extname COLLATE "C";

SELECT json_build_object('kind', 'schema', 'name', n.nspname, 'owner', pg_get_userbyid(n.nspowner),
    'acl', ARRAY(SELECT x::text FROM unnest(coalesce(n.nspacl, acldefault('n', n.nspowner))) AS x
        ORDER BY x::text COLLATE "C"))
FROM pg_namespace AS n
WHERE n.oid IN :user_namespaces
ORDER BY n.nspname COLLATE "C";

SELECT json_build_object('kind', 'relation', 'name', c.oid::regclass::text, 'relkind', c.relkind,
    'owner', pg_get_userbyid(c.relowner), 'persistence', c.relpersistence,
    'acl', ARRAY(SELECT x::text FROM unnest(coalesce(c.relacl,
        acldefault(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) AS x
        ORDER BY x::text COLLATE "C"),
    'options', ARRAY(SELECT x FROM unnest(c.reloptions) AS x ORDER BY x COLLATE "C"),
    'row_security', c.relrowsecurity, 'force_row_security', c.relforcerowsecurity,
    'replica_identity', c.relreplident,
    'parents', ARRAY(SELECT i.inhparent::regclass::text FROM pg_inherits AS i
        WHERE i.inhrelid = c.oid ORDER BY i.inhseqno),
    'partition_bound', pg_get_expr(c.relpartbound, c.oid),
    'columns', (
        SELECT json_agg(json_build_object('name', a.attname,
            'type', format_type(a.atttypid, a.atttypmod), 'not_null', a.attnotnull,
            'default', pg_get_expr(ad.adbin, ad.adrelid), 'storage', a.attstorage,
            'compression', a.attcompression, 'identity', a.attidentity,
            'generated', a.attgenerated, 'statistics', a.attstattarget,
            'collation', CASE WHEN a.attcollation <> 0 THEN a.attcollation::regcollation::text END,
            'acl', ARRAY(SELECT x::text FROM unnest(a.attacl) AS x ORDER BY x::text COLLATE "C"))
            ORDER BY a.attnum)
        FROM pg_attribute AS a
        LEFT JOIN pg_attrdef AS ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped))
FROM pg_class AS c
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c') AND c.relnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND ((d.classid = 'pg_class'::regclass AND d.objid = c.oid)
            OR (d.classid = 'pg_type'::regclass AND d.objid = c.reltype)))
ORDER BY c.oid::regclass::text COLLATE "C";

SELECT json_build_object('kind', 'sequence_definition', 'name', c.oid::regclass::text,
    'type', format_type(s.seqtypid, NULL), 'start', s.seqstart, 'min', s.seqmin,
    'max', s.seqmax, 'increment', s.seqincrement, 'cycle', s.seqcycle, 'cache', s.seqcache)
FROM pg_sequence AS s
JOIN pg_class AS c ON c.oid = s.seqrelid
WHERE c.relnamespace IN :user_namespaces
ORDER BY c.oid::regclass::text COLLATE "C";

SELECT json_build_object('kind', 'index', 'name', i.indexrelid::regclass::text,
    'table', i.indrelid::regclass::text, 'definition', pg_get_indexdef(i.indexrelid),
    'valid', i.indisvalid, 'ready', i.indisready, 'clustered', i.indisclustered,
    'replica_identity', i.indisreplident)
FROM pg_index AS i
JOIN pg_class AS c ON c.oid = i.indrelid
WHERE c.relnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_class'::regclass AND d.objid = i.indrelid)
ORDER BY i.indexrelid::regclass::text COLLATE "C";

SELECT json_build_object('kind', 'constraint', 'on', con.on_object, 'name', con.conname,
    'type', con.contype, 'definition', pg_get_constraintdef(con.oid),
    'validated', con.convalidated, 'deferrable', con.condeferrable, 'deferred', con.condeferred)
FROM (
    SELECT co.*,
        coalesce(nullif(co.conrelid, 0)::regclass::text, co.contypid::regtype::text) AS on_object
    FROM pg_constraint AS co
    WHERE co.connamespace IN :user_namespaces
) AS con
ORDER BY con.on_object COLLATE "C", con.conname COLLATE "C";

SELECT json_build_object('kind', 'trigger', 'table', t.tgrelid::regclass::text, 'name', t.tgname,
    'definition', pg_get_triggerdef(t.oid), 'enabled', t.tgenabled)
FROM pg_trigger AS t
JOIN pg_class AS c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal AND c.relnamespace IN :user_namespaces
ORDER BY t.tgrelid::regclass::text COLLATE "C", t.tgname COLLATE "C";

SELECT json_build_object('kind', 'policy', 'table', p.polrelid::regclass::text, 'name', p.polname,
    'command', p.polcmd, 'permissive', p.polpermissive,
    'roles', ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE pg_get_userbyid(r)::text END
        FROM unnest(p.polroles) AS r ORDER BY 1),
    'using', pg_get_expr(p.polqual, p.polrelid),
    'check', pg_get_expr(p.polwithcheck, p.polrelid))
FROM pg_policy AS p
JOIN pg_class AS c ON c.oid = p.polrelid
WHERE c.relnamespace IN :user_namespaces
ORDER BY p.polrelid::regclass::text COLLATE "C", p.polname COLLATE "C";

SELECT json_build_object('kind', 'function', 'signature', p.oid::regprocedure::text,
    'prokind', p.prokind, 'owner', pg_get_userbyid(p.proowner),
    'acl', ARRAY(SELECT x::text FROM unnest(coalesce(p.proacl, acldefault('f', p.proowner))) AS x
        ORDER BY x::text COLLATE "C"),
    'security_definer', p.prosecdef, 'leakproof', p.proleakproof, 'volatility', p.provolatile,
    'parallel', p.proparallel,
    'config', ARRAY(SELECT x FROM unnest(p.proconfig) AS x ORDER BY x COLLATE "C"),
    'definition_md5', CASE WHEN p.prokind <> 'a' THEN md5(pg_get_functiondef(p.oid)) END)
FROM pg_proc AS p
WHERE p.pronamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_proc'::regclass AND d.objid = p.oid)
ORDER BY p.oid::regprocedure::text COLLATE "C";

SELECT json_build_object('kind', 'view', 'name', c.oid::regclass::text,
    'definition_md5', md5(pg_get_viewdef(c.oid)))
FROM pg_class AS c
WHERE c.relkind IN ('v', 'm') AND c.relnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_class'::regclass AND d.objid = c.oid)
ORDER BY c.oid::regclass::text COLLATE "C";

SELECT json_build_object('kind', 'type', 'name', t.oid::regtype::text, 'typtype', t.typtype,
    'owner', pg_get_userbyid(t.typowner),
    'acl', ARRAY(SELECT x::text FROM unnest(coalesce(t.typacl, acldefault('T', t.typowner))) AS x
        ORDER BY x::text COLLATE "C"),
    'base', CASE WHEN t.typtype = 'd' THEN format_type(t.typbasetype, t.typtypmod) END,
    'labels', CASE WHEN t.typtype = 'e' THEN ARRAY(SELECT e.enumlabel::text FROM pg_enum AS e
        WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder) END)
FROM pg_type AS t
WHERE t.typnamespace IN :user_namespaces AND t.typtype IN ('d', 'e', 'r', 'm')
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_type'::regclass AND d.objid = t.oid)
ORDER BY t.oid::regtype::text COLLATE "C";

SELECT json_build_object('kind', 'ts_config', 'name', cfg.oid::regconfig::text,
    'owner', pg_get_userbyid(cfg.cfgowner),
    'parser', (SELECT prs.prsname FROM pg_ts_parser AS prs WHERE prs.oid = cfg.cfgparser),
    'mapping', ARRAY(
        SELECT m.maptokentype || ':' || m.mapseqno || ':' || m.mapdict::regdictionary::text
        FROM pg_ts_config_map AS m WHERE m.mapcfg = cfg.oid
        ORDER BY m.maptokentype, m.mapseqno))
FROM pg_ts_config AS cfg
WHERE cfg.cfgnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_ts_config'::regclass AND d.objid = cfg.oid)
ORDER BY cfg.oid::regconfig::text COLLATE "C";

SELECT json_build_object('kind', 'ts_dictionary', 'name', dict.oid::regdictionary::text,
    'owner', pg_get_userbyid(dict.dictowner),
    'template', (SELECT tmpl.tmplname FROM pg_ts_template AS tmpl
        WHERE tmpl.oid = dict.dicttemplate),
    'options', dict.dictinitoption)
FROM pg_ts_dict AS dict
WHERE dict.dictnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_ts_dict'::regclass AND d.objid = dict.oid)
ORDER BY dict.oid::regdictionary::text COLLATE "C";

SELECT json_build_object('kind', 'default_acl', 'role', pg_get_userbyid(da.defaclrole),
    'schema', CASE WHEN da.defaclnamespace <> 0 THEN da.defaclnamespace::regnamespace::text END,
    'objtype', da.defaclobjtype,
    'acl', ARRAY(SELECT x::text FROM unnest(da.defaclacl) AS x ORDER BY x::text COLLATE "C"))
FROM pg_default_acl AS da
ORDER BY pg_get_userbyid(da.defaclrole)::text COLLATE "C",
    da.defaclnamespace::regnamespace::text COLLATE "C", da.defaclobjtype;

\if :catalog_only
\else
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age') AS has_age \gset
\if :has_age
-- AGE keeps its catalog in extension configuration tables, which only data dumps carry. Compared
-- by name: ag_graph.graphid is a bare OID that a restore does not keep (apache/age#2503).
SELECT json_build_object('kind', 'age_graph', 'name', g.name, 'namespace', g.namespace::text)
FROM ag_catalog.ag_graph AS g
ORDER BY g.name::text COLLATE "C";

SELECT json_build_object('kind', 'age_label', 'graph', g.name, 'label', l.name, 'id', l.id,
    'label_kind', l.kind, 'relation', l.relation::text, 'sequence', l.seq_name)
FROM ag_catalog.ag_label AS l
JOIN ag_catalog.ag_graph AS g ON g.graphid = l.graph
ORDER BY g.name::text COLLATE "C", l.name::text COLLATE "C";
\endif

SELECT json_build_object('kind', 'sequence_value', 'name', c.oid::regclass::text,
    'last_value', CASE WHEN has_sequence_privilege(c.oid, 'SELECT,USAGE')
        THEN pg_sequence_last_value(c.oid) END)
FROM pg_class AS c
WHERE c.relkind = 'S' AND c.relnamespace IN :user_namespaces
ORDER BY c.oid::regclass::text COLLATE "C";

-- Row count and an order-independent digest of every row's text form, per table.
SELECT format(
    'SELECT json_build_object(%L, %L, %L, %L, %L, count(*), %L, '
    'md5(coalesce(string_agg(s.h, %L ORDER BY s.h COLLATE "C"), %L))) '
    'FROM (SELECT md5(manifest_row::text) AS h FROM %s AS manifest_row) AS s',
    'kind', 'table_data', 'name', c.oid::regclass::text, 'rows', 'digest', '', '',
    c.oid::regclass)
FROM pg_class AS c
WHERE c.relkind = 'r' AND c.relnamespace IN :user_namespaces
    AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
        AND d.classid = 'pg_class'::regclass AND d.objid = c.oid)
ORDER BY c.oid::regclass::text COLLATE "C"
\gexec
\endif

COMMIT;
