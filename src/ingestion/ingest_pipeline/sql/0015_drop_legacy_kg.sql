-- The copied Tizert maintenance graph (migrations 0011-0013: schema kg, urn:tizert: IRIs and the
-- tizert_kg_* roles) is replaced by the CRV research graph (crv_kg, 0016-0017). The earlier
-- migrations stay registered with their checksums; this removes what they created.
DROP SCHEMA IF EXISTS kg CASCADE;

DO $drop$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['tizert_kg_reader', 'tizert_kg_writer', 'tizert_kg_release']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            BEGIN
                EXECUTE format('DROP ROLE %I', role_name);
            EXCEPTION WHEN dependent_objects_still_exist OR insufficient_privilege THEN
                -- Privileges held in another database keep the role; it has no objects here.
                RAISE NOTICE 'Role % kept: %', role_name, SQLERRM;
            END;
        END IF;
    END LOOP;
END
$drop$;
