-- 0006_ingest_projection_content: projection rows are shared by every placement of a content.
-- Items record their content so a content's rows can be found (and retired) without a join;
-- placement_id becomes the canonical placement that names the row. Requires 0004.

DO $migration$
BEGIN
    IF to_regclass('ingest.projection_item') IS NULL THEN
        RAISE EXCEPTION '0004_ingest_evidence must be applied before 0006';
    END IF;
END
$migration$;

ALTER TABLE ingest.projection_item ADD COLUMN content_hash TEXT;

UPDATE ingest.projection_item AS item
SET content_hash = document.content_hash
FROM ingest.evidence_document AS document
WHERE document.evidence_id = item.evidence_id;

ALTER TABLE ingest.projection_item
    ALTER COLUMN content_hash SET NOT NULL,
    ADD CONSTRAINT projection_item_content_hash_check CHECK (content_hash ~ '^[0-9a-f]{64}$');

CREATE INDEX projection_item_content_idx
    ON ingest.projection_item (store, target, content_hash, state);
