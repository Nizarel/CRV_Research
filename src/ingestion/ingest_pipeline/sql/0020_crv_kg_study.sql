-- KG-2 pilot (Docs/KNOWLEDGE_GRAPH_DESIGN.md §3, §5-6): studies and samples are entities linked
-- by assertions (Document reports Study, Study uses Sample...); observations stay relational
-- (n-ary, queried with SQL) with their own append-only status history and release membership.
-- Every LLM or table-rule record starts as a candidate; vocabularies mirror
-- ingest_pipeline/domain/crv_kg/study.py and vocab.py (checked by a unit test).
ALTER TABLE crv_kg.entity DROP CONSTRAINT entity_kind_check;
ALTER TABLE crv_kg.entity ADD CONSTRAINT entity_kind_check CHECK (kind IN ('Document',
    'VersionFamily', 'ReportCode', 'Organisation', 'Site', 'Project', 'Collection', 'Folder',
    'Study', 'Sample'));

ALTER TABLE crv_kg.assertion DROP CONSTRAINT assertion_predicate_check;
ALTER TABLE crv_kg.assertion ADD CONSTRAINT assertion_predicate_check CHECK (predicate IN (
    'hasCode', 'inCollection', 'filedUnder', 'aboutSite', 'concerns', 'issuedBy', 'attributedTo',
    'aboutProject', 'memberOf', 'supersedes', 'duplicateOf', 'partOf', 'operatedBy', 'reports',
    'uses', 'performedAt', 'performedBy', 'commissionedBy', 'follows', 'validates'));

ALTER TABLE crv_kg.assertion DROP CONSTRAINT assertion_method_check;
ALTER TABLE crv_kg.assertion ADD CONSTRAINT assertion_method_check CHECK (method IN ('path',
    'filename', 'cover', 'header_code', 'printed_code', 'title', 'revision', 'gazetteer', 'llm',
    'table'));

-- One measured or reported value, as printed, with the quote that states it (evidence row,
-- locator_type text_span: {"page": n, "quote": "..."}). value_num is parsed from value_text,
-- never generated; subject_text keeps an unresolved sample or site name as printed.
CREATE TABLE crv_kg.observation (
    iri TEXT PRIMARY KEY CHECK (iri ~ '^urn:crv:obs:[0-9a-f]{64}$'),
    document_iri TEXT NOT NULL REFERENCES crv_kg.entity (iri),
    study_iri TEXT REFERENCES crv_kg.entity (iri),
    sample_iri TEXT REFERENCES crv_kg.entity (iri),
    subject_text TEXT CHECK (length(subject_text) <= 200),
    property TEXT NOT NULL CHECK (property IN ('head-grade', 'oxidation-rate', 'recovery',
        'concentrate-grade', 'distribution', 'weight-yield', 'work-index', 'd80',
        'acid-consumption', 'reagent-consumption', 'ph', 'cu-fe-ratio', 'tonnage', 'throughput',
        'resource-tonnage', 'cost', 'energy', 'mineral-abundance')),
    element TEXT CHECK (length(element) <= 40),
    stream TEXT CHECK (stream IN ('feed', 'concentrate', 'rougher-concentrate',
        'cleaner-concentrate', 'scavenger', 'middlings', 'tailings', 'final', 'leach-solution',
        'residue')),
    basis TEXT CHECK (basis IN ('dry', 'wet', 'per-t-tv', 'kwh-per-t', 'kwh-per-sht',
        'mine-to-cathode', 'reagents-only', 'opex', 'capex', 'cut-off')),
    value_text TEXT NOT NULL CHECK (value_text <> '' AND length(value_text) <= 120),
    value_num NUMERIC,
    unit TEXT CHECK (length(unit) <= 40),
    qualifier TEXT CHECK (qualifier IN ('<', '<=', '>', '>=', '~')),
    method TEXT NOT NULL CHECK (method IN ('llm', 'table')),
    evidence_iri TEXT NOT NULL REFERENCES crv_kg.evidence (iri),
    generated_by_run TEXT NOT NULL REFERENCES crv_kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX observation_property_idx ON crv_kg.observation (property, stream);
CREATE INDEX observation_document_idx ON crv_kg.observation (document_iri);
CREATE INDEX observation_study_idx ON crv_kg.observation (study_iri);

CREATE TABLE crv_kg.observation_status (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_iri TEXT NOT NULL REFERENCES crv_kg.observation (iri),
    status TEXT NOT NULL CHECK (status IN ('observed', 'candidate', 'approved', 'rejected',
        'superseded')),
    decision_id TEXT,
    run_id TEXT REFERENCES crv_kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL,
    CHECK (status NOT IN ('approved', 'rejected') OR decision_id IS NOT NULL),
    CHECK (decision_id IS NOT NULL OR run_id IS NOT NULL)
);

CREATE INDEX observation_status_latest_idx
    ON crv_kg.observation_status (observation_iri, seq DESC);

CREATE VIEW crv_kg.observation_current AS
SELECT DISTINCT ON (observation_iri) observation_iri, status, decision_id, run_id, recorded_at
FROM crv_kg.observation_status
ORDER BY observation_iri, seq DESC;

-- Observations frozen into a release (the read API never sees later, unreleased runs).
CREATE TABLE crv_kg.release_observation (
    release_id TEXT NOT NULL REFERENCES crv_kg.release (release_id),
    observation_iri TEXT NOT NULL REFERENCES crv_kg.observation (iri),
    status TEXT NOT NULL,
    PRIMARY KEY (release_id, observation_iri)
);

GRANT SELECT, INSERT ON crv_kg.observation, crv_kg.observation_status TO crv_kg_writer;
GRANT SELECT ON crv_kg.observation_current TO crv_kg_writer;
GRANT SELECT ON crv_kg.observation, crv_kg.observation_status, crv_kg.observation_current,
    crv_kg.release_observation TO crv_kg_release;
GRANT INSERT ON crv_kg.release_observation TO crv_kg_release;
