-- Ingestion catalog schema (Architecture 1, Section 3 entity model).
-- Applied to the `ingestion-catalog` database on the shared Postgres Flexible Server.
-- One content_hash is extracted once; every folder placement is retained (never coalesced).

CREATE TABLE IF NOT EXISTS source_object (
    content_hash TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    blob_name TEXT,
    archive_parent_id TEXT,
    archive_entry_path TEXT,
    size_bytes BIGINT NOT NULL,
    extension TEXT NOT NULL,
    mime_type TEXT,
    last_modified TIMESTAMPTZ,
    etag TEXT,
    ingestion_state TEXT NOT NULL DEFAULT 'discovered',
    route TEXT NOT NULL,
    quarantine_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS placement (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES source_object (content_hash),
    source_object_id TEXT NOT NULL,
    source_path_raw TEXT NOT NULL,
    zone_name TEXT,
    section TEXT,
    primary_equipment_tag TEXT,
    related_equipment_tags TEXT[] NOT NULL DEFAULT '{}',
    document_category_raw TEXT,
    document_class TEXT NOT NULL DEFAULT 'unclassified',
    metadata_source TEXT NOT NULL DEFAULT 'path',
    metadata_confidence TEXT NOT NULL DEFAULT 'medium',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_placement_content_hash ON placement (content_hash);
CREATE INDEX IF NOT EXISTS idx_placement_primary_equipment_tag ON placement (primary_equipment_tag);

-- Only a governed document register may ever set is_current; never inferred from a filename.
CREATE TABLE IF NOT EXISTS logical_document (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES source_object (content_hash),
    document_code TEXT,
    revision_normalized TEXT,
    submittal TEXT,
    status TEXT,
    is_current BOOLEAN NOT NULL DEFAULT false,
    metadata_source TEXT,
    metadata_confidence TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_logical_document_content_hash ON logical_document (content_hash);
CREATE INDEX IF NOT EXISTS idx_logical_document_document_code ON logical_document (document_code);

CREATE TABLE IF NOT EXISTS page (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES source_object (content_hash),
    page_number INTEGER NOT NULL,
    width DOUBLE PRECISION,
    height DOUBLE PRECISION,
    unit TEXT,
    angle DOUBLE PRECISION,
    page_image_uri TEXT,
    UNIQUE (content_hash, page_number)
);

CREATE TABLE IF NOT EXISTS figure (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES source_object (content_hash),
    figure_id TEXT NOT NULL,
    page_number INTEGER,
    caption TEXT,
    crop_blob_name TEXT,
    UNIQUE (content_hash, figure_id)
);

CREATE TABLE IF NOT EXISTS chunk (
    chunk_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES source_object (content_hash),
    document_class TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    page_number INTEGER,
    keywords TEXT[] NOT NULL DEFAULT '{}',
    indexed_at TIMESTAMPTZ,
    UNIQUE (content_hash, ordinal)
);

CREATE TABLE IF NOT EXISTS deadletter (
    id BIGSERIAL PRIMARY KEY,
    content_hash TEXT,
    source_object_id TEXT NOT NULL,
    lane TEXT,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deadletter_content_hash ON deadletter (content_hash);

-- Ingestion state per content_hash; source_object.ingestion_state is the first-seen value,
-- this table is the authoritative, updatable current state.
CREATE TABLE IF NOT EXISTS state (
    content_hash TEXT PRIMARY KEY REFERENCES source_object (content_hash),
    ingestion_state TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
