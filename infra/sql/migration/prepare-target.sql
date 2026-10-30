-- Prepare a new, empty DGX staging database for pg_restore from a source inventory (superuser).
--   psql -X -At -v ON_ERROR_STOP=1 -v target_db=<name> -d postgres -f prepare-target.sql \
--       < inventory.jsonl
-- Creates every role the dump references as a NOLOGIN placeholder (no password, no attributes),
-- their memberships, the database with the source encoding, locale and owner, and the database
-- grants and settings that pg_dump does not carry. Refuses a database that already exists.
\set ON_ERROR_STOP on
\set QUIET on
SET search_path = pg_catalog;
\if :{?target_db}
\else
\warn 'prepare-target.sql: pass -v target_db=<name>'
DO $missing$ BEGIN RAISE EXCEPTION 'target_db is required'; END $missing$;
\endif
SELECT set_config('crv.target_db', :'target_db', false) AS target_db_set \gset

CREATE TEMP TABLE inventory_line (line text);
-- One JSON document per line: \x01 and \x02 never occur in JSON text.
\copy inventory_line FROM pstdin WITH (FORMAT csv, QUOTE E'\x01', DELIMITER E'\x02')
CREATE TEMP TABLE inventory AS
SELECT line::jsonb AS j FROM inventory_line WHERE btrim(line) <> '';
-- Log every statement from here on (the generated ones included).
\set ECHO queries

DO $check$
DECLARE
    target text := current_setting('crv.target_db');
BEGIN
    IF (SELECT count(*) FROM inventory WHERE j->>'kind' = 'database') <> 1 THEN
        RAISE EXCEPTION 'The inventory must describe exactly one database';
    END IF;
    IF (SELECT (j->>'version_num')::integer / 10000 FROM inventory WHERE j->>'kind' = 'server')
            <> current_setting('server_version_num')::integer / 10000 THEN
        RAISE EXCEPTION 'The source and target PostgreSQL major versions differ';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_database WHERE datname = target) THEN
        RAISE EXCEPTION 'Database % already exists; restore into a new database', target;
    END IF;
    IF EXISTS (SELECT 1 FROM inventory WHERE j->>'kind' = 'unreadable') THEN
        RAISE EXCEPTION 'The export identity could not read every object; see "unreadable"';
    END IF;
END
$check$;

SELECT format('CREATE ROLE %I NOLOGIN %s', j->>'name',
    CASE WHEN (j->>'inherit')::boolean THEN 'INHERIT' ELSE 'NOINHERIT' END)
FROM inventory
WHERE j->>'kind' = 'role' AND NOT (j->>'predefined')::boolean
    AND NOT EXISTS (SELECT 1 FROM pg_roles AS r WHERE r.rolname = j->>'name')
ORDER BY j->>'name' COLLATE "C"
\gexec

-- Memberships in predefined roles are platform grants (Azure gives azure_pg_admin several,
-- including roles PostgreSQL 16 does not have): placeholders get none of them.
SELECT format('GRANT %I TO %I WITH ADMIN %s, INHERIT %s, SET %s', j->>'role', j->>'member',
    upper(j->>'admin_option'), upper(j->>'inherit_option'), upper(j->>'set_option'))
FROM inventory
WHERE j->>'kind' = 'membership' AND j->>'role' !~ '^pg_' AND j->>'member' !~ '^pg_'
    AND NOT EXISTS (SELECT 1 FROM pg_auth_members AS m
        WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname = j->>'role')
            AND m.member = (SELECT oid FROM pg_roles WHERE rolname = j->>'member'))
ORDER BY j->>'role' COLLATE "C", j->>'member' COLLATE "C"
\gexec

SELECT format('CREATE DATABASE %I WITH OWNER %I TEMPLATE template0 ENCODING %L %s CONNECTION LIMIT %s',
    current_setting('crv.target_db'), j->>'owner', j->>'encoding',
    CASE j->>'locale_provider'
        WHEN 'i' THEN format('LOCALE_PROVIDER icu ICU_LOCALE %L LC_COLLATE %L LC_CTYPE %L',
            j->>'icu_locale', j->>'collate', j->>'ctype')
        ELSE format('LOCALE_PROVIDER libc LC_COLLATE %L LC_CTYPE %L', j->>'collate', j->>'ctype')
    END,
    (j->>'connection_limit')::integer)
FROM inventory
WHERE j->>'kind' = 'database'
\gexec

-- A source ACL replaces the defaults (owner: all; PUBLIC: CONNECT, TEMPORARY).
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_setting('crv.target_db'))
FROM inventory
WHERE j->>'kind' = 'database' AND jsonb_typeof(j->'acl') = 'array'
\gexec

SELECT format('GRANT %s ON DATABASE %I TO %s%s', a.privilege_type,
    current_setting('crv.target_db'),
    CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
    CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END)
FROM inventory AS i,
    aclexplode(ARRAY(SELECT jsonb_array_elements_text(i.j->'acl'))::aclitem[]) AS a
WHERE i.j->>'kind' = 'database' AND jsonb_typeof(i.j->'acl') = 'array'
    AND a.grantee <> (SELECT datdba FROM pg_database
        WHERE datname = current_setting('crv.target_db'))
ORDER BY 1
\gexec

-- List-valued settings keep their raw form; everything else is quoted as one value.
SELECT CASE
        WHEN s.role IS NULL THEN format('ALTER DATABASE %I SET %s',
            current_setting('crv.target_db'), q.assignment)
        ELSE format('ALTER ROLE %I IN DATABASE %I SET %s', s.role,
            current_setting('crv.target_db'), q.assignment)
    END
FROM (
    SELECT i.j->>'role' AS role, x.item
    FROM inventory AS i, jsonb_array_elements_text(i.j->'settings') AS x(item)
    WHERE i.j->>'kind' = 'db_setting'
) AS s,
LATERAL (SELECT split_part(s.item, '=', 1) AS name,
    substr(s.item, strpos(s.item, '=') + 1) AS value) AS kv,
LATERAL (SELECT CASE
    WHEN kv.name IN ('search_path', 'temp_tablespaces', 'local_preload_libraries',
        'session_preload_libraries') THEN format('%s = %s', kv.name, kv.value)
    ELSE format('%s = %L', kv.name, kv.value) END AS assignment) AS q
ORDER BY 1
\gexec

SELECT json_build_object('kind', 'prepared', 'database', current_setting('crv.target_db'),
    'placeholder_roles', (SELECT count(*) FROM inventory WHERE j->>'kind' = 'role'
        AND NOT (j->>'predefined')::boolean),
    'memberships', (SELECT count(*) FROM inventory WHERE j->>'kind' = 'membership'),
    'database_settings', (SELECT count(*) FROM inventory WHERE j->>'kind' = 'db_setting'));
