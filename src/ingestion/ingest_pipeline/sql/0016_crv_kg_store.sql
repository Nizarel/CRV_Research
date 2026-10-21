-- CRV research knowledge graph, deterministic tier (Docs/KNOWLEDGE_GRAPH_DESIGN.md §6): an
-- append-only system of record (runs, entities, evidence, assertions and their status history)
-- and immutable releases projected to relational vertex/edge tables and to an Apache AGE graph
-- per release. The relational projection is authoritative for access: every vertex and edge
-- carries the scope keys of its evidence.
--
-- AGE must be allow-listed (azure.extensions) and preloaded (shared_preload_libraries) on the
-- server first; never run LOAD 'age' on Azure.
CREATE EXTENSION IF NOT EXISTS age;

CREATE SCHEMA crv_kg;

CREATE TABLE crv_kg.run (
    run_id TEXT PRIMARY KEY CHECK (run_id ~ '^urn:crv:run:[0-9a-f]{64}$'),
    kind TEXT NOT NULL CHECK (kind IN ('build', 'release', 'review')),
    tool TEXT NOT NULL CHECK (tool <> ''),
    tool_version TEXT NOT NULL CHECK (tool_version <> ''),
    input_hash TEXT NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL CHECK (completed_at >= started_at)
);

CREATE TABLE crv_kg.entity (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:crv:[a-z]+:[^[:space:]]+$'),
    kind TEXT NOT NULL CHECK (kind IN ('Document', 'VersionFamily', 'ReportCode', 'Organisation',
        'Site', 'Project', 'Collection', 'Folder')),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'retired')),
    created_by_run TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    created_at TIMESTAMPTZ NOT NULL
);

-- What each run reported about an entity: short as-printed attributes, never page text.
CREATE TABLE crv_kg.entity_record (
    entity_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    run_id TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    review_status TEXT NOT NULL CHECK (review_status IN ('observed', 'candidate', 'approved',
        'rejected', 'superseded')),
    label TEXT NOT NULL,
    attributes JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (entity_iri, run_id)
);

CREATE INDEX entity_record_latest_idx ON crv_kg.entity_record (entity_iri, recorded_at DESC);

CREATE TABLE crv_kg.identifier (
    entity_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    scheme TEXT NOT NULL CHECK (scheme IN ('content_hash', 'report_code', 'source_path',
        'file_name', 'folder', 'alias', 'code_s3', 'name')),
    value_raw TEXT NOT NULL CHECK (value_raw <> ''),
    value_normalized TEXT NOT NULL CHECK (value_normalized <> ''),
    run_id TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    PRIMARY KEY (entity_iri, scheme, value_normalized)
);

CREATE INDEX identifier_lookup_idx ON crv_kg.identifier (scheme, value_normalized);

-- One locator per row: a file path, a page text span, page-header codes or a gazetteer entry.
-- scope_key is the access scope of the source (per collection; crv:gazetteer for the seed).
CREATE TABLE crv_kg.evidence (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:crv:evidence:[0-9a-f]{64}$'),
    source_hash TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    source_name TEXT NOT NULL CHECK (source_name <> ''),
    locator_type TEXT NOT NULL CHECK (locator_type IN ('file_path', 'text_span', 'page_header',
        'gazetteer')),
    locator JSONB NOT NULL,
    scope_key TEXT NOT NULL CHECK (scope_key ~ '^crv:[a-z][a-z0-9-]*$'),
    run_id TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE crv_kg.entity_evidence (
    entity_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    evidence_iri TEXT NOT NULL REFERENCES crv_kg.evidence (iri),
    run_id TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    PRIMARY KEY (entity_iri, evidence_iri)
);

CREATE TABLE crv_kg.assertion (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:crv:assertion:[0-9a-f]{64}$'),
    subject_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    predicate TEXT NOT NULL CHECK (predicate IN ('hasCode', 'inCollection', 'filedUnder',
        'aboutSite', 'concerns', 'issuedBy', 'attributedTo', 'aboutProject', 'memberOf',
        'supersedes', 'duplicateOf', 'partOf', 'operatedBy')),
    object_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    method TEXT NOT NULL CHECK (method IN ('path', 'filename', 'cover', 'header_code',
        'printed_code', 'title', 'revision', 'gazetteer')),
    generated_by_run TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX assertion_subject_idx ON crv_kg.assertion (subject_iri, predicate);
CREATE INDEX assertion_object_idx ON crv_kg.assertion (object_iri, predicate);

-- Append-only status history; an approval or rejection by an expert names its decision.
CREATE TABLE crv_kg.assertion_status (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assertion_iri TEXT NOT NULL REFERENCES crv_kg.assertion (iri),
    status TEXT NOT NULL CHECK (status IN ('observed', 'candidate', 'approved', 'rejected',
        'superseded')),
    decision_id TEXT,
    run_id TEXT REFERENCES crv_kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL,
    CHECK (status NOT IN ('approved', 'rejected') OR decision_id IS NOT NULL),
    CHECK (decision_id IS NOT NULL OR run_id IS NOT NULL)
);

CREATE INDEX assertion_status_latest_idx ON crv_kg.assertion_status (assertion_iri, seq DESC);

CREATE VIEW crv_kg.assertion_current AS
SELECT DISTINCT ON (assertion_iri) assertion_iri, status, decision_id, run_id, recorded_at
FROM crv_kg.assertion_status
ORDER BY assertion_iri, seq DESC;

CREATE TABLE crv_kg.assertion_evidence (
    assertion_iri TEXT NOT NULL REFERENCES crv_kg.assertion (iri),
    evidence_iri TEXT NOT NULL REFERENCES crv_kg.evidence (iri),
    run_id TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    PRIMARY KEY (assertion_iri, evidence_iri)
);

-- Releases are immutable snapshots of the projection; one manifest hash, one release.
CREATE TABLE crv_kg.release (
    release_id TEXT PRIMARY KEY CHECK (release_id ~ '^crvkg-[0-9]{8}-[0-9a-f]{12}$'),
    manifest_sha256 TEXT NOT NULL UNIQUE CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    projection_version TEXT NOT NULL,
    statuses TEXT[] NOT NULL CHECK (cardinality(statuses) > 0),
    vertex_count INTEGER NOT NULL CHECK (vertex_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    built_by_run TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE crv_kg.release_member (
    release_id TEXT NOT NULL REFERENCES crv_kg.release (release_id),
    assertion_iri TEXT NOT NULL REFERENCES crv_kg.assertion (iri),
    status TEXT NOT NULL,
    PRIMARY KEY (release_id, assertion_iri)
);

CREATE TABLE crv_kg.vertex_projection (
    release_id TEXT NOT NULL REFERENCES crv_kg.release (release_id),
    iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    label TEXT NOT NULL CHECK (label ~ '^[A-Z][A-Za-z]+$'),
    properties JSONB NOT NULL,
    scope_keys TEXT[] NOT NULL CHECK (cardinality(scope_keys) > 0),
    PRIMARY KEY (release_id, iri)
);

CREATE INDEX vertex_projection_label_idx ON crv_kg.vertex_projection (release_id, label);

CREATE TABLE crv_kg.edge_projection (
    release_id TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label ~ '^[A-Z][A-Z_]+$'),
    start_iri TEXT NOT NULL,
    end_iri TEXT NOT NULL,
    properties JSONB NOT NULL,
    scope_keys TEXT[] NOT NULL CHECK (cardinality(scope_keys) > 0),
    assertion_iris TEXT[] NOT NULL CHECK (cardinality(assertion_iris) > 0),
    PRIMARY KEY (release_id, label, start_iri, end_iri),
    FOREIGN KEY (release_id, start_iri) REFERENCES crv_kg.vertex_projection (release_id, iri),
    FOREIGN KEY (release_id, end_iri) REFERENCES crv_kg.vertex_projection (release_id, iri)
);

CREATE INDEX edge_projection_start_idx ON crv_kg.edge_projection (release_id, start_iri, label);
CREATE INDEX edge_projection_end_idx ON crv_kg.edge_projection (release_id, end_iri, label);

CREATE TABLE crv_kg.release_graph (
    release_id TEXT PRIMARY KEY REFERENCES crv_kg.release (release_id),
    graph_name TEXT NOT NULL UNIQUE CHECK (graph_name ~ '^crv_research_[0-9a-f]{12}$'),
    vertex_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);

-- Activation history; the current release per environment is the latest row.
CREATE TABLE crv_kg.release_pointer (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment ~ '^[a-z][a-z0-9-]*$'),
    release_id TEXT NOT NULL REFERENCES crv_kg.release (release_id),
    activated_by TEXT NOT NULL DEFAULT current_user,
    activated_at TIMESTAMPTZ NOT NULL
);

CREATE VIEW crv_kg.release_active AS
SELECT DISTINCT ON (pointer.environment) pointer.environment, pointer.release_id,
    graph.graph_name, pointer.activated_at
FROM crv_kg.release_pointer AS pointer
LEFT JOIN crv_kg.release_graph AS graph USING (release_id)
ORDER BY pointer.environment, pointer.seq DESC;

-- The build job records runs and what they report; nothing recorded is updated or deleted.
-- The release job freezes projections. Entra principals are mapped to these roles by an
-- administrator (db-bootstrap --kg-principal).
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crv_kg_writer') THEN
        CREATE ROLE crv_kg_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crv_kg_release') THEN
        CREATE ROLE crv_kg_release NOLOGIN;
    END IF;
END
$roles$;

GRANT USAGE ON SCHEMA crv_kg TO crv_kg_writer, crv_kg_release;
GRANT SELECT, INSERT ON crv_kg.run, crv_kg.entity, crv_kg.entity_record, crv_kg.identifier,
    crv_kg.evidence, crv_kg.entity_evidence, crv_kg.assertion, crv_kg.assertion_status,
    crv_kg.assertion_evidence TO crv_kg_writer;
GRANT SELECT ON crv_kg.assertion_current TO crv_kg_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA crv_kg TO crv_kg_release;
GRANT INSERT ON crv_kg.run, crv_kg.release, crv_kg.release_member, crv_kg.vertex_projection,
    crv_kg.edge_projection, crv_kg.release_graph, crv_kg.release_pointer TO crv_kg_release;
-- The build reads what was ingested: evidence documents and their source placements.
GRANT USAGE ON SCHEMA ingest TO crv_kg_writer;
GRANT SELECT ON ingest.evidence_document, ingest.source_snapshot TO crv_kg_writer;
