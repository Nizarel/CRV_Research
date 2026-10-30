-- Repair Apache AGE graph ids after pg_restore (apache/age#2503). ag_graph.graphid and
-- ag_label.graph are bare OIDs that keep the source database's namespace OIDs, so Cypher fails
-- with "graph with oid ... does not exist" until they equal ag_graph.namespace again.
-- Idempotent; superuser, on the restored database only. Prints one JSON summary line.
--   psql -X -At -v ON_ERROR_STOP=1 -d <database> -f age-graphid-repair.sql
\set ON_ERROR_STOP on
\set QUIET on
SET search_path = pg_catalog;
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age') AS has_age \gset
\if :has_age
BEGIN;
LOCK TABLE ag_catalog.ag_graph, ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE;

CREATE TEMP TABLE age_graph_map ON COMMIT DROP AS
SELECT g.graphid AS old_id, g.namespace::oid AS new_id
FROM ag_catalog.ag_graph AS g
WHERE g.graphid IS DISTINCT FROM g.namespace::oid;

DO $repair$
DECLARE
    fk_name name;
    fk_definition text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM age_graph_map) THEN
        RETURN;
    END IF;
    SELECT c.conname, pg_get_constraintdef(c.oid) INTO fk_name, fk_definition
    FROM pg_constraint AS c
    WHERE c.conrelid = 'ag_catalog.ag_label'::regclass AND c.contype = 'f'
        AND c.confrelid = 'ag_catalog.ag_graph'::regclass;
    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT %I', fk_name);
    END IF;
    -- Rewrite both catalogs whole: updating in place can collide on their unique indexes when
    -- one graph's new id equals another graph's stale id.
    CREATE TEMP TABLE age_label_fixed ON COMMIT DROP AS SELECT * FROM ag_catalog.ag_label;
    UPDATE age_label_fixed AS l SET graph = m.new_id
    FROM age_graph_map AS m WHERE l.graph = m.old_id;
    CREATE TEMP TABLE age_graph_fixed ON COMMIT DROP AS SELECT * FROM ag_catalog.ag_graph;
    UPDATE age_graph_fixed AS g SET graphid = m.new_id
    FROM age_graph_map AS m WHERE g.graphid = m.old_id;
    DELETE FROM ag_catalog.ag_label;
    DELETE FROM ag_catalog.ag_graph;
    INSERT INTO ag_catalog.ag_graph SELECT * FROM age_graph_fixed;
    INSERT INTO ag_catalog.ag_label SELECT * FROM age_label_fixed;
    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT %I %s',
            fk_name, fk_definition);
    END IF;
END
$repair$;

SELECT json_build_object('kind', 'age_graphid_repair',
    'graphs', (SELECT count(*) FROM ag_catalog.ag_graph),
    'repaired', (SELECT count(*) FROM age_graph_map),
    'stale', (SELECT count(*) FROM ag_catalog.ag_graph AS g
        WHERE g.graphid IS DISTINCT FROM g.namespace::oid),
    'orphan_labels', (SELECT count(*) FROM ag_catalog.ag_label AS l
        WHERE NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph AS g WHERE g.graphid = l.graph)));

DO $verify$
BEGIN
    IF EXISTS (SELECT 1 FROM ag_catalog.ag_graph AS g
            WHERE g.graphid IS DISTINCT FROM g.namespace::oid)
        OR EXISTS (SELECT 1 FROM ag_catalog.ag_label AS l
            WHERE NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph AS g WHERE g.graphid = l.graph))
    THEN
        RAISE EXCEPTION 'AGE graph ids are still inconsistent; rolled back';
    END IF;
END
$verify$;
COMMIT;
\else
SELECT json_build_object('kind', 'age_graphid_repair', 'graphs', 0, 'repaired', 0, 'stale', 0,
    'orphan_labels', 0);
\endif
