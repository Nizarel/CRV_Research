-- Edge logins and the empty agent-memory database on the restored DGX server (superuser).
-- Run through infra/dgx/edge/edge-logins.sh, which passes the passwords in the environment:
--   CRV_AGENT_PASSWORD     crv_edge_agent: backend runtime (KG reader via SET ROLE, memory DML)
--   CRV_MIGRATOR_PASSWORD  crv_edge_migrator: owns crv-agent-memory, runs setup-memory
-- Neither login may own, or be a member of the owner of, the restored ingestion-catalog.
-- Memory tables and their DML grants come from `crv-agent setup-memory` and
-- `crv-agent grant-memory-runtime --login crv_edge_agent`, run as the migrator. Idempotent.
\set ON_ERROR_STOP on
\set QUIET on
SET search_path = pg_catalog;
\getenv agent_pw CRV_AGENT_PASSWORD
\getenv migrator_pw CRV_MIGRATOR_PASSWORD
\if :{?agent_pw}
\else
DO $missing$ BEGIN RAISE EXCEPTION 'CRV_AGENT_PASSWORD is not set'; END $missing$;
\endif
\if :{?migrator_pw}
\else
DO $missing$ BEGIN RAISE EXCEPTION 'CRV_MIGRATOR_PASSWORD is not set'; END $missing$;
\endif
-- Keep the passwords out of the server log, including the text of a failing statement.
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = 'panic';

DO $check$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ingestion-catalog') THEN
        RAISE EXCEPTION 'Restore ingestion-catalog before creating the edge logins';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crv_kg_reader') THEN
        RAISE EXCEPTION 'The restored crv_kg_reader role is missing';
    END IF;
END
$check$;

SELECT format('CREATE ROLE %I LOGIN', name)
FROM (VALUES ('crv_edge_agent'), ('crv_edge_migrator')) AS v(name)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v.name)
\gexec

ALTER ROLE crv_edge_agent WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS INHERIT CONNECTION LIMIT 40 PASSWORD :'agent_pw';
ALTER ROLE crv_edge_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS INHERIT CONNECTION LIMIT 4 PASSWORD :'migrator_pw';
RESET log_min_error_statement;

-- The backend runs SET ROLE crv_kg_reader on KG connections; the login itself inherits nothing.
GRANT crv_kg_reader TO crv_edge_agent WITH INHERIT FALSE, SET TRUE;
GRANT CONNECT ON DATABASE "ingestion-catalog" TO crv_edge_agent;

SELECT 'CREATE DATABASE "crv-agent-memory" WITH OWNER crv_edge_migrator TEMPLATE template0 '
    || 'ENCODING ''UTF8'' LOCALE_PROVIDER libc LC_COLLATE ''en_US.utf8'' LC_CTYPE ''en_US.utf8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'crv-agent-memory')
\gexec
REVOKE ALL ON DATABASE "crv-agent-memory" FROM PUBLIC;
GRANT CONNECT ON DATABASE "crv-agent-memory" TO crv_edge_agent;

DO $verify$
DECLARE
    catalog_owner oid := (SELECT datdba FROM pg_database WHERE datname = 'ingestion-catalog');
    login text;
BEGIN
    FOREACH login IN ARRAY ARRAY['crv_edge_agent', 'crv_edge_migrator'] LOOP
        IF pg_has_role(login, catalog_owner, 'MEMBER') THEN
            RAISE EXCEPTION '% is a member of the ingestion-catalog owner role', login;
        END IF;
        IF (SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = login)
        THEN
            RAISE EXCEPTION '% has administrative attributes', login;
        END IF;
        IF has_database_privilege(login, 'ingestion-catalog', 'CREATE') THEN
            RAISE EXCEPTION '% can create schemas in ingestion-catalog', login;
        END IF;
    END LOOP;
END
$verify$;

SELECT json_build_object(
    'kind', 'edge_logins',
    'agent', 'crv_edge_agent', 'migrator', 'crv_edge_migrator',
    'agent_sets_kg_reader', (SELECT set_option FROM pg_auth_members
        WHERE roleid = 'crv_kg_reader'::regrole AND member = 'crv_edge_agent'::regrole),
    'agent_inherits_kg_reader', (SELECT inherit_option FROM pg_auth_members
        WHERE roleid = 'crv_kg_reader'::regrole AND member = 'crv_edge_agent'::regrole),
    'memory_owner', (SELECT pg_get_userbyid(datdba) FROM pg_database
        WHERE datname = 'crv-agent-memory'));
