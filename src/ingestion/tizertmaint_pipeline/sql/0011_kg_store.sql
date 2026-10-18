-- Knowledge-graph system of record (Docs/knowledge-graph/03-storage-architecture.md §4) and the
-- release projection that feeds AGE and its SQL fallback (§5). First slice: register imports.
-- Review decisions, ACL tables and document evidence arrive in later migrations.

CREATE TABLE kg.run (
    run_id TEXT PRIMARY KEY CHECK (run_id ~ '^urn:tizert:run:[0-9a-f]{64}$'),
    kind TEXT NOT NULL CHECK (kind IN ('import', 'extraction', 'llm', 'release')),
    tool TEXT NOT NULL CHECK (tool <> ''),
    tool_version TEXT NOT NULL CHECK (tool_version <> ''),
    input_hash TEXT NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL CHECK (completed_at >= started_at)
);

CREATE TABLE kg.entity (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:tizert:[a-z]+:'),
    kind TEXT NOT NULL CHECK (kind IN ('Site', 'ProcessGroup', 'ProcessArea', 'Equipment',
        'InstrumentTag', 'EntityCandidate', 'DocumentReference', 'Concept')),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'tombstoned', 'retired')),
    replaced_by TEXT REFERENCES kg.entity (iri),
    created_by_run TEXT NOT NULL REFERENCES kg.run (run_id),
    created_at TIMESTAMPTZ NOT NULL
);

-- What each run reported about an entity; raw attributes are lossless, not verified facts.
CREATE TABLE kg.entity_record (
    entity_iri TEXT NOT NULL REFERENCES kg.entity (iri),
    run_id TEXT NOT NULL REFERENCES kg.run (run_id),
    review_status TEXT NOT NULL CHECK (review_status IN ('observed', 'reported', 'candidate',
        'rejected', 'superseded')),
    identifier TEXT NOT NULL,
    label TEXT NOT NULL,
    raw_record JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (entity_iri, run_id)
);

CREATE TABLE kg.identifier (
    entity_iri TEXT NOT NULL REFERENCES kg.entity (iri),
    scheme TEXT NOT NULL CHECK (scheme IN ('site', 'process_group', 'area_code', 'equipment_tag',
        'instrument_tag', 'document_file', 'legend_prefix')),
    value_raw TEXT NOT NULL CHECK (value_raw <> ''),
    value_normalized TEXT NOT NULL CHECK (value_normalized <> ''),
    status TEXT NOT NULL CHECK (status IN ('reported', 'candidate', 'approved', 'rejected')),
    decision_id TEXT,
    run_id TEXT NOT NULL REFERENCES kg.run (run_id),
    PRIMARY KEY (entity_iri, scheme, value_normalized),
    CHECK (status <> 'approved' OR decision_id IS NOT NULL)
);

CREATE UNIQUE INDEX identifier_approved_uq ON kg.identifier (scheme, value_normalized)
    WHERE status = 'approved';
CREATE INDEX identifier_lookup_idx ON kg.identifier (scheme, value_normalized);

-- scope_key is the access scope of the source (all register CSVs belong to 'registers').
CREATE TABLE kg.evidence (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:tizert:evidence:'),
    source_hash TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    source_name TEXT NOT NULL CHECK (source_name <> ''),
    locator_type TEXT NOT NULL CHECK (locator_type IN ('csv_row', 'page', 'page_region',
        'table_cell', 'sheet_cell', 'text_span', 'native')),
    locator JSONB NOT NULL,
    scope_key TEXT NOT NULL CHECK (scope_key ~ '^[a-z][a-z0-9_.:-]*$'),
    run_id TEXT NOT NULL REFERENCES kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE kg.entity_evidence (
    entity_iri TEXT NOT NULL REFERENCES kg.entity (iri),
    evidence_iri TEXT NOT NULL REFERENCES kg.evidence (iri),
    run_id TEXT NOT NULL REFERENCES kg.run (run_id),
    PRIMARY KEY (entity_iri, evidence_iri)
);

CREATE TABLE kg.assertion (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:tizert:assertion:'),
    subject_iri TEXT NOT NULL REFERENCES kg.entity (iri),
    predicate TEXT NOT NULL CHECK (predicate IN ('inSite', 'inProcessGroup', 'reportedInArea',
        'tagOf', 'referencesDocument', 'measuresMonitorsOrControls',
        'actuatesOrFinalElementFor')),
    object_iri TEXT NOT NULL REFERENCES kg.entity (iri),
    method TEXT NOT NULL CHECK (method <> ''),
    generated_by_run TEXT NOT NULL REFERENCES kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX assertion_subject_idx ON kg.assertion (subject_iri, predicate);
CREATE INDEX assertion_object_idx ON kg.assertion (object_iri, predicate);

-- Append-only status history; an approval always names its review decision.
CREATE TABLE kg.assertion_status (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assertion_iri TEXT NOT NULL REFERENCES kg.assertion (iri),
    status TEXT NOT NULL CHECK (status IN ('observed', 'reported', 'candidate', 'approved',
        'rejected', 'superseded')),
    decision_id TEXT,
    run_id TEXT REFERENCES kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL,
    CHECK (status <> 'approved' OR decision_id IS NOT NULL),
    CHECK (decision_id IS NOT NULL OR run_id IS NOT NULL)
);

CREATE INDEX assertion_status_latest_idx ON kg.assertion_status (assertion_iri, seq DESC);

CREATE VIEW kg.assertion_current AS
SELECT DISTINCT ON (assertion_iri) assertion_iri, status, decision_id, run_id, recorded_at
FROM kg.assertion_status
ORDER BY assertion_iri, seq DESC;

CREATE TABLE kg.assertion_evidence (
    assertion_iri TEXT NOT NULL REFERENCES kg.assertion (iri),
    evidence_iri TEXT NOT NULL REFERENCES kg.evidence (iri),
    role TEXT NOT NULL CHECK (role IN ('supports', 'contradicts')),
    run_id TEXT NOT NULL REFERENCES kg.run (run_id),
    PRIMARY KEY (assertion_iri, evidence_iri, role)
);

-- Releases are immutable snapshots of the projection; one manifest hash, one release.
CREATE TABLE kg.release (
    release_id TEXT PRIMARY KEY CHECK (release_id ~ '^kg-[0-9]{8}-[0-9a-f]{12}$'),
    manifest_sha256 TEXT NOT NULL UNIQUE CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    projection_version TEXT NOT NULL,
    statuses TEXT[] NOT NULL CHECK (cardinality(statuses) > 0),
    vertex_count INTEGER NOT NULL CHECK (vertex_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    built_by_run TEXT NOT NULL REFERENCES kg.run (run_id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE kg.release_member (
    release_id TEXT NOT NULL REFERENCES kg.release (release_id),
    assertion_iri TEXT NOT NULL REFERENCES kg.assertion (iri),
    status TEXT NOT NULL,
    PRIMARY KEY (release_id, assertion_iri)
);

CREATE TABLE kg.vertex_projection (
    release_id TEXT NOT NULL REFERENCES kg.release (release_id),
    iri TEXT NOT NULL REFERENCES kg.entity (iri),
    label TEXT NOT NULL CHECK (label ~ '^[A-Z][A-Za-z]+$'),
    properties JSONB NOT NULL,
    scope_keys TEXT[] NOT NULL CHECK (cardinality(scope_keys) > 0),
    PRIMARY KEY (release_id, iri)
);

CREATE TABLE kg.edge_projection (
    release_id TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label ~ '^[A-Z][A-Z_]+$'),
    start_iri TEXT NOT NULL,
    end_iri TEXT NOT NULL,
    properties JSONB NOT NULL,
    scope_keys TEXT[] NOT NULL CHECK (cardinality(scope_keys) > 0),
    assertion_iris TEXT[] NOT NULL CHECK (cardinality(assertion_iris) > 0),
    PRIMARY KEY (release_id, label, start_iri, end_iri),
    FOREIGN KEY (release_id, start_iri) REFERENCES kg.vertex_projection (release_id, iri),
    FOREIGN KEY (release_id, end_iri) REFERENCES kg.vertex_projection (release_id, iri)
);

CREATE INDEX edge_projection_start_idx ON kg.edge_projection (release_id, start_iri, label);
CREATE INDEX edge_projection_end_idx ON kg.edge_projection (release_id, end_iri, label);

CREATE TABLE kg.release_graph (
    release_id TEXT PRIMARY KEY REFERENCES kg.release (release_id),
    graph_name TEXT NOT NULL UNIQUE CHECK (graph_name ~ '^kgr_[0-9a-f]{12}$'),
    vertex_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL
);

-- Activation history; the current release per environment is the latest row.
CREATE TABLE kg.release_pointer (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment ~ '^[a-z][a-z0-9-]*$'),
    release_id TEXT NOT NULL REFERENCES kg.release (release_id),
    activated_by TEXT NOT NULL DEFAULT current_user,
    activated_at TIMESTAMPTZ NOT NULL
);

CREATE VIEW kg.release_active AS
SELECT DISTINCT ON (pointer.environment) pointer.environment, pointer.release_id,
    graph.graph_name, pointer.activated_at
FROM kg.release_pointer AS pointer
LEFT JOIN kg.release_graph AS graph USING (release_id)
ORDER BY pointer.environment, pointer.seq DESC;

-- Writers import evidence and assertions; nothing in the record of what was reported is
-- updated or deleted. Entra principals are mapped to these roles by an administrator.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tizert_kg_writer') THEN
        CREATE ROLE tizert_kg_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tizert_kg_release') THEN
        CREATE ROLE tizert_kg_release NOLOGIN;
    END IF;
END
$roles$;

GRANT USAGE ON SCHEMA kg TO tizert_kg_writer, tizert_kg_release;
GRANT SELECT, INSERT ON kg.run, kg.entity, kg.entity_record, kg.identifier, kg.evidence,
    kg.entity_evidence, kg.assertion, kg.assertion_status, kg.assertion_evidence
    TO tizert_kg_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO tizert_kg_release;
GRANT INSERT ON kg.run, kg.release, kg.release_member, kg.vertex_projection,
    kg.edge_projection, kg.release_graph, kg.release_pointer TO tizert_kg_release;
