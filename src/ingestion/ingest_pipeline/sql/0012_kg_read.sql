-- Release-pinned, read-only knowledge-graph API for the agent
-- (Docs/knowledge-graph/03-storage-architecture.md §6.2). Each function reads one immutable
-- release, applies the caller's access scopes to every vertex, edge and evidence row before
-- ranking and limits, and runs as its owner, so tizert_kg_reader needs EXECUTE only.
-- Invisible equals unknown: nothing reports whether an out-of-scope entity exists.
-- Raw attributes, legend concepts and provenance are read as recorded at release time.

-- Run configs recorded before this migration were JSON-encoded twice (a JSON string holding
-- the object); store the object they describe.
UPDATE kg.run SET config = (config #>> '{}')::jsonb WHERE jsonb_typeof(config) = 'string';

-- Curated aliases (data/knowledge/agent/aliases.json). Each import is a new set; the latest
-- set is current. An alias may name a tag that is missing from the register.
CREATE TABLE kg.alias_set (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES kg.run (run_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE kg.alias (
    run_id TEXT NOT NULL REFERENCES kg.alias_set (run_id),
    alias TEXT NOT NULL CHECK (btrim(alias) <> ''),
    target_tag TEXT NOT NULL CHECK (target_tag ~ '^[0-9]{4}-[A-Z]{1,5}-[0-9]{3,4}[A-Z]?$'),
    label TEXT,
    note TEXT,
    PRIMARY KEY (run_id, alias, target_tag)
);

-- Case-, accent- and punctuation-free key: '6730ya6381' and '6730-YA-6381' meet.
CREATE FUNCTION kg.compact_key(value TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN upper(regexp_replace(normalize(value, NFKD), '[^A-Za-z0-9]+', '', 'g'));

-- Lower-case ASCII words in order (accents folded, other non-ASCII dropped).
CREATE FUNCTION kg.words(value TEXT) RETURNS TEXT[]
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN ARRAY(
        SELECT found.word[1]
        FROM regexp_matches(lower(regexp_replace(normalize(value, NFKD), '[^\x01-\x7f]', '', 'g')),
            '[a-z0-9]+', 'g') WITH ORDINALITY AS found(word, n)
        ORDER BY found.n);

-- Distinct words that carry meaning for name matching.
CREATE FUNCTION kg.content_words(value TEXT) RETURNS TEXT[]
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN ARRAY(
        SELECT DISTINCT word FROM unnest(kg.words(value)) AS word
        WHERE word <> ALL (ARRAY['the', 'and', 'for', 'with', 'from', 'into', 'unit', 'system',
            'des', 'les', 'une', 'pour', 'sur', 'dans', 'avec', 'par', 'est', 'quel', 'quelle',
            'quels', 'quelles', 'comment', 'combien', 'what', 'which', 'how'])
        ORDER BY word);

CREATE INDEX vertex_projection_key_idx
    ON kg.vertex_projection (release_id, kg.compact_key(properties ->> 'identifier'));

-- The release an environment currently serves.
CREATE FUNCTION kg.active_release(p_environment TEXT)
    RETURNS TABLE (release_id TEXT, graph_name TEXT, activated_at TIMESTAMPTZ)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT active.release_id, active.graph_name, active.activated_at
    FROM kg.release_active AS active
    WHERE active.environment = p_environment
$$;

-- Entities named in free text: tag-shaped mentions first (first mention first), then curated
-- alias phrases; equipment-name word overlap only when neither is present. A mention whose
-- tag is not a visible vertex comes back with a NULL iri. Name matches with a score within
-- 15% of the best are all returned and flagged ambiguous.
CREATE FUNCTION kg.resolve(p_release TEXT, p_text TEXT, p_scopes TEXT[], p_limit INTEGER DEFAULT 5)
    RETURNS TABLE (rank INTEGER, mention TEXT, method TEXT, score NUMERIC, ambiguous BOOLEAN,
        iri TEXT, kind TEXT, identifier TEXT, name TEXT, status TEXT, alias_label TEXT,
        alias_note TEXT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH params AS (
        SELECT least(greatest(coalesce(p_limit, 5), 1), 20) AS cap,
            kg.words(coalesce(p_text, '')) AS words,
            kg.content_words(coalesce(p_text, '')) AS asked
    ),
    tags AS (
        SELECT DISTINCT ON (found.tag) found.tag, found.n
        FROM (
            SELECT m.part[1] || '-' || m.part[2] || '-' || m.part[3] AS tag, m.n
            FROM regexp_matches(upper(coalesce(p_text, '')),
                '(?<![A-Z0-9])([0-9]{4})-?([A-Z]{1,5})-?([0-9]{3,4}[A-Z]?)(?![A-Z0-9])', 'g')
                WITH ORDINALITY AS m(part, n)
        ) AS found
        ORDER BY found.tag, found.n
    ),
    aliases AS (
        SELECT a.alias, a.target_tag, a.label, a.note
        FROM kg.alias AS a, params
        WHERE a.run_id = (SELECT s.run_id FROM kg.alias_set AS s ORDER BY s.seq DESC LIMIT 1)
            AND cardinality(kg.words(a.alias)) > 0
            AND ' ' || array_to_string(params.words, ' ') || ' '
                LIKE '% ' || array_to_string(kg.words(a.alias), ' ') || ' %'
    ),
    explicit AS (
        SELECT DISTINCT ON (mention.target) mention.*
        FROM (
            SELECT 1 AS source, t.n AS position, '' AS sort_key, t.tag AS mention,
                'tag' AS method, t.tag AS target, NULL::text AS alias_label,
                NULL::text AS alias_note
            FROM tags AS t
            UNION ALL
            SELECT 2, 0, lower(a.alias), a.alias, 'alias', a.target_tag, a.label, a.note
            FROM aliases AS a
        ) AS mention
        ORDER BY mention.target, mention.source, mention.position, mention.sort_key COLLATE "C"
    ),
    explicit_ranked AS (
        SELECT row_number() OVER (ORDER BY e.source, e.position, e.sort_key COLLATE "C",
                e.target COLLATE "C")::integer AS rank,
            e.mention, e.method, 1.0::numeric AS score, false AS ambiguous,
            v.iri, v.label AS kind, coalesce(v.properties ->> 'identifier', e.target) AS identifier,
            coalesce(v.properties ->> 'name', e.alias_label) AS name,
            v.properties ->> 'status' AS status, e.alias_label, e.alias_note
        FROM explicit AS e
        LEFT JOIN LATERAL (
            SELECT vp.iri, vp.label, vp.properties
            FROM kg.vertex_projection AS vp
            WHERE vp.release_id = p_release
                AND kg.compact_key(vp.properties ->> 'identifier') = kg.compact_key(e.target)
                AND vp.label IN ('Equipment', 'InstrumentTag')
                AND vp.scope_keys && p_scopes
            ORDER BY vp.label, vp.iri
            LIMIT 1
        ) AS v ON true
    ),
    named AS (
        SELECT vp.iri, vp.properties, kg.content_words(vp.properties ->> 'name') AS words
        FROM kg.vertex_projection AS vp
        WHERE vp.release_id = p_release AND vp.label = 'Equipment' AND vp.scope_keys && p_scopes
            AND NOT EXISTS (SELECT 1 FROM explicit)
    ),
    scored AS (
        SELECT n.iri, n.properties, overlap.hits,
            overlap.hits::numeric / cardinality(n.words) AS raw_score
        FROM named AS n, params,
            LATERAL (SELECT count(*) AS hits FROM unnest(n.words) AS word
                WHERE word = ANY (params.asked)) AS overlap
        WHERE cardinality(n.words) > 0
    ),
    best AS (
        SELECT s.*, row_number() OVER (ORDER BY s.raw_score DESC,
                s.properties ->> 'identifier' COLLATE "C", s.iri)::integer AS rank
        FROM scored AS s
        WHERE s.hits >= 2 AND s.raw_score >= 0.6
    ),
    kept AS (
        SELECT b.* FROM best AS b, params WHERE b.rank <= params.cap
    ),
    flag AS (
        SELECT count(*) > 1 AND (max(k.raw_score) FILTER (WHERE k.rank = 1)
                - max(k.raw_score) FILTER (WHERE k.rank = 2))
                / max(k.raw_score) FILTER (WHERE k.rank = 1) < 0.15 AS ambiguous
        FROM kept AS k
    )
    SELECT r.rank, r.mention, r.method, r.score, r.ambiguous, r.iri, r.kind, r.identifier,
        r.name, r.status, r.alias_label, r.alias_note
    FROM explicit_ranked AS r, params
    WHERE r.rank <= params.cap
    UNION ALL
    SELECT k.rank, k.properties ->> 'name', 'name', round(k.raw_score, 3), coalesce(f.ambiguous, false),
        k.iri, 'Equipment', k.properties ->> 'identifier', k.properties ->> 'name',
        k.properties ->> 'status', NULL, NULL
    FROM kept AS k, flag AS f
    WHERE f.ambiguous OR k.rank = 1
    ORDER BY 1
$$;

-- Exact identifier lookup for any kind (area code, process group, document file, tag).
CREATE FUNCTION kg.lookup(p_release TEXT, p_value TEXT, p_scopes TEXT[],
        p_kinds TEXT[] DEFAULT NULL, p_limit INTEGER DEFAULT 10)
    RETURNS TABLE (iri TEXT, kind TEXT, identifier TEXT, name TEXT, status TEXT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT vp.iri, vp.label, vp.properties ->> 'identifier', vp.properties ->> 'name',
        vp.properties ->> 'status'
    FROM kg.vertex_projection AS vp
    WHERE vp.release_id = p_release
        AND kg.compact_key(vp.properties ->> 'identifier') = kg.compact_key(p_value)
        AND kg.compact_key(p_value) <> ''
        AND (p_kinds IS NULL OR vp.label = ANY (p_kinds))
        AND vp.scope_keys && p_scopes
    ORDER BY vp.label COLLATE "C", vp.properties ->> 'identifier' COLLATE "C", vp.iri
    LIMIT least(greatest(coalesce(p_limit, 10), 1), 50)
$$;

-- One visible entity with the raw register attributes and the sources the caller may see.
CREATE FUNCTION kg.entity(p_release TEXT, p_iri TEXT, p_scopes TEXT[])
    RETURNS TABLE (iri TEXT, kind TEXT, identifier TEXT, name TEXT, status TEXT,
        attributes JSONB, sources JSONB)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT vp.iri, vp.label, vp.properties ->> 'identifier', vp.properties ->> 'name',
        vp.properties ->> 'status', record.raw_record, coalesce(source.items, '[]'::jsonb)
    FROM kg.vertex_projection AS vp
    JOIN kg.release AS rel ON rel.release_id = vp.release_id
    LEFT JOIN LATERAL (
        SELECT r.raw_record
        FROM kg.entity_record AS r
        JOIN kg.run AS run ON run.run_id = r.run_id
        WHERE r.entity_iri = vp.iri AND r.recorded_at <= rel.created_at
            AND run.config ->> 'scope_key' = ANY (p_scopes)
        ORDER BY r.recorded_at DESC, r.run_id DESC
        LIMIT 1
    ) AS record ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('evidence_iri', ev.iri,
                'source_name', ev.source_name, 'source_hash', ev.source_hash,
                'row_number', ev.locator -> 'row_number')
            ORDER BY ev.source_name COLLATE "C", (ev.locator ->> 'row_number')::integer, ev.iri)
            AS items
        FROM (
            SELECT e.* FROM kg.entity_evidence AS link
            JOIN kg.evidence AS e ON e.iri = link.evidence_iri
            WHERE link.entity_iri = vp.iri AND e.scope_key = ANY (p_scopes)
                AND e.recorded_at <= rel.created_at
            ORDER BY e.source_name COLLATE "C", (e.locator ->> 'row_number')::integer, e.iri
            LIMIT 20
        ) AS ev
    ) AS source ON true
    WHERE vp.release_id = p_release AND vp.iri = p_iri AND vp.scope_keys && p_scopes
$$;

-- One-hop neighbours of a visible vertex, optionally for one relation and direction, at most
-- p_limit per (relation, direction) ordered by identifier; total counts every visible one.
CREATE FUNCTION kg.neighbors(p_release TEXT, p_iri TEXT, p_scopes TEXT[],
        p_relation TEXT DEFAULT NULL, p_direction TEXT DEFAULT NULL, p_limit INTEGER DEFAULT 25)
    RETURNS TABLE (relation TEXT, direction TEXT, iri TEXT, kind TEXT, identifier TEXT,
        name TEXT, entity_status TEXT, status TEXT, assertion_iris TEXT[], total BIGINT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH start AS (
        SELECT vp.iri FROM kg.vertex_projection AS vp
        WHERE vp.release_id = p_release AND vp.iri = p_iri AND vp.scope_keys && p_scopes
    ),
    edges AS (
        SELECT e.label, 'out' AS direction, e.end_iri AS neighbor, e.properties ->> 'status' AS status,
            e.assertion_iris
        FROM kg.edge_projection AS e JOIN start ON e.start_iri = start.iri
        WHERE e.release_id = p_release AND e.scope_keys && p_scopes
            AND (p_relation IS NULL OR e.label = p_relation)
            AND coalesce(p_direction, 'out') = 'out'
        UNION ALL
        SELECT e.label, 'in', e.start_iri, e.properties ->> 'status', e.assertion_iris
        FROM kg.edge_projection AS e JOIN start ON e.end_iri = start.iri
        WHERE e.release_id = p_release AND e.scope_keys && p_scopes
            AND (p_relation IS NULL OR e.label = p_relation)
            AND coalesce(p_direction, 'in') = 'in'
    ),
    visible AS (
        SELECT ed.label, ed.direction, ed.neighbor, ed.status, ed.assertion_iris,
            v.label AS kind, v.properties,
            count(*) OVER (PARTITION BY ed.label, ed.direction) AS total,
            row_number() OVER (PARTITION BY ed.label, ed.direction
                ORDER BY v.properties ->> 'identifier' COLLATE "C", v.iri) AS position
        FROM edges AS ed
        JOIN kg.vertex_projection AS v
            ON v.release_id = p_release AND v.iri = ed.neighbor AND v.scope_keys && p_scopes
    )
    SELECT vis.label, vis.direction, vis.neighbor, vis.kind, vis.properties ->> 'identifier',
        vis.properties ->> 'name', vis.properties ->> 'status', vis.status, vis.assertion_iris,
        vis.total
    FROM visible AS vis
    WHERE vis.position <= least(greatest(coalesce(p_limit, 25), 1), 50)
    ORDER BY vis.label, vis.direction, vis.position
$$;

-- Containment chain above a visible vertex (tag -> equipment -> area -> group -> site), at
-- most four hops; every hop must be visible.
CREATE FUNCTION kg.lineage(p_release TEXT, p_iri TEXT, p_scopes TEXT[])
    RETURNS TABLE (depth INTEGER, relation TEXT, from_iri TEXT, iri TEXT, kind TEXT,
        identifier TEXT, name TEXT, entity_status TEXT, status TEXT, assertion_iris TEXT[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH RECURSIVE chain AS (
        SELECT 0 AS depth, NULL::text AS relation, NULL::text AS from_iri, vp.iri,
            NULL::text AS status, NULL::text[] AS assertion_iris, ARRAY[vp.iri] AS path
        FROM kg.vertex_projection AS vp
        WHERE vp.release_id = p_release AND vp.iri = p_iri AND vp.scope_keys && p_scopes
        UNION ALL
        SELECT c.depth + 1, e.label, c.iri, e.end_iri, e.properties ->> 'status',
            e.assertion_iris, c.path || e.end_iri
        FROM chain AS c
        JOIN kg.edge_projection AS e
            ON e.release_id = p_release AND e.start_iri = c.iri
            AND e.label IN ('TAG_OF', 'REPORTED_IN_AREA', 'IN_PROCESS_GROUP', 'IN_SITE')
            AND e.scope_keys && p_scopes
        JOIN kg.vertex_projection AS v
            ON v.release_id = p_release AND v.iri = e.end_iri AND v.scope_keys && p_scopes
        WHERE c.depth < 4 AND e.end_iri <> ALL (c.path)
    )
    SELECT c.depth, c.relation, c.from_iri, c.iri, v.label, v.properties ->> 'identifier',
        v.properties ->> 'name', v.properties ->> 'status', c.status, c.assertion_iris
    FROM chain AS c
    JOIN kg.vertex_projection AS v ON v.release_id = p_release AND v.iri = c.iri
    WHERE c.depth > 0
    ORDER BY c.depth, c.relation, v.properties ->> 'identifier' COLLATE "C", c.iri
    LIMIT 100
$$;

-- Declared links between two visible vertices; direction 'out' means p_a -> p_b.
CREATE FUNCTION kg.relations_between(p_release TEXT, p_a TEXT, p_b TEXT, p_scopes TEXT[])
    RETURNS TABLE (relation TEXT, direction TEXT, status TEXT, assertion_iris TEXT[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT e.label, CASE WHEN e.start_iri = p_a THEN 'out' ELSE 'in' END,
        e.properties ->> 'status', e.assertion_iris
    FROM kg.edge_projection AS e
    JOIN kg.vertex_projection AS a
        ON a.release_id = p_release AND a.iri = p_a AND a.scope_keys && p_scopes
    JOIN kg.vertex_projection AS b
        ON b.release_id = p_release AND b.iri = p_b AND b.scope_keys && p_scopes
    WHERE e.release_id = p_release AND e.scope_keys && p_scopes
        AND ((e.start_iri = p_a AND e.end_iri = p_b) OR (e.start_iri = p_b AND e.end_iri = p_a))
    ORDER BY 1, 2
$$;

-- Supporting evidence of release assertions (at most 100 asked, 500 rows returned), with the
-- status each assertion entered the release with. Both endpoints must be visible.
CREATE FUNCTION kg.provenance(p_release TEXT, p_assertions TEXT[], p_scopes TEXT[])
    RETURNS TABLE (assertion_iri TEXT, predicate TEXT, subject_iri TEXT, object_iri TEXT,
        status TEXT, evidence_iri TEXT, source_name TEXT, source_hash TEXT, source_row INTEGER,
        locator JSONB)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT a.iri, a.predicate, a.subject_iri, a.object_iri, member.status, ev.iri,
        ev.source_name, ev.source_hash, (ev.locator ->> 'row_number')::integer, ev.locator
    FROM (SELECT DISTINCT wanted FROM unnest(p_assertions[1:100]) AS wanted) AS asked
    JOIN kg.release_member AS member
        ON member.release_id = p_release AND member.assertion_iri = asked.wanted
    JOIN kg.release AS rel ON rel.release_id = member.release_id
    JOIN kg.assertion AS a ON a.iri = member.assertion_iri
    JOIN kg.vertex_projection AS s
        ON s.release_id = p_release AND s.iri = a.subject_iri AND s.scope_keys && p_scopes
    JOIN kg.vertex_projection AS o
        ON o.release_id = p_release AND o.iri = a.object_iri AND o.scope_keys && p_scopes
    JOIN kg.assertion_evidence AS link ON link.assertion_iri = a.iri AND link.role = 'supports'
    JOIN kg.evidence AS ev ON ev.iri = link.evidence_iri AND ev.scope_key = ANY (p_scopes)
        AND ev.recorded_at <= rel.created_at
    ORDER BY a.iri, ev.source_name COLLATE "C", (ev.locator ->> 'row_number')::integer, ev.iri
    LIMIT 500
$$;

-- Imported legend meanings of a tag prefix. Legend concepts are candidates, not release
-- vertices, so they keep their own status and are read as recorded at release time.
CREATE FUNCTION kg.legend(p_release TEXT, p_prefix TEXT, p_scopes TEXT[])
    RETURNS TABLE (iri TEXT, prefix TEXT, name TEXT, class TEXT, signal TEXT,
        interpretation TEXT, notes TEXT, status TEXT, attributes JSONB, sources JSONB)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT concept.iri, record.raw_record ->> 'Code / Prefix', record.raw_record ->> 'Name',
        record.raw_record ->> 'Class', record.raw_record ->> 'Type of Signal',
        record.raw_record ->> 'Interpretation Used in Workbook',
        record.raw_record ->> 'Review Notes', record.review_status, record.raw_record,
        source.items
    FROM kg.release AS rel
    JOIN kg.identifier AS ident
        ON ident.scheme = 'legend_prefix' AND ident.value_normalized = lower(btrim(p_prefix))
        AND ident.status <> 'rejected'
    JOIN kg.run AS ident_run
        ON ident_run.run_id = ident.run_id AND ident_run.completed_at <= rel.created_at
    JOIN kg.entity AS concept
        ON concept.iri = ident.entity_iri AND concept.kind = 'Concept' AND concept.state = 'active'
    JOIN LATERAL (
        SELECT r.raw_record, r.review_status
        FROM kg.entity_record AS r
        JOIN kg.run AS run ON run.run_id = r.run_id
        WHERE r.entity_iri = concept.iri AND r.recorded_at <= rel.created_at
            AND run.config ->> 'scope_key' = ANY (p_scopes)
        ORDER BY r.recorded_at DESC, r.run_id DESC
        LIMIT 1
    ) AS record ON true
    JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('evidence_iri', ev.iri,
                'source_name', ev.source_name, 'source_hash', ev.source_hash,
                'row_number', ev.locator -> 'row_number')
            ORDER BY ev.source_name COLLATE "C", (ev.locator ->> 'row_number')::integer, ev.iri)
            AS items
        FROM kg.entity_evidence AS link
        JOIN kg.evidence AS ev ON ev.iri = link.evidence_iri
        WHERE link.entity_iri = concept.iri AND ev.scope_key = ANY (p_scopes)
            AND ev.recorded_at <= rel.created_at
    ) AS source ON source.items IS NOT NULL
    WHERE rel.release_id = p_release
    ORDER BY record.raw_record ->> 'Name' COLLATE "C", concept.iri
    LIMIT 20
$$;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tizert_kg_reader') THEN
        CREATE ROLE tizert_kg_reader NOLOGIN;
    END IF;
END
$roles$;

REVOKE ALL ON FUNCTION kg.active_release(TEXT), kg.resolve(TEXT, TEXT, TEXT[], INTEGER),
    kg.lookup(TEXT, TEXT, TEXT[], TEXT[], INTEGER), kg.entity(TEXT, TEXT, TEXT[]),
    kg.neighbors(TEXT, TEXT, TEXT[], TEXT, TEXT, INTEGER), kg.lineage(TEXT, TEXT, TEXT[]),
    kg.relations_between(TEXT, TEXT, TEXT, TEXT[]), kg.provenance(TEXT, TEXT[], TEXT[]),
    kg.legend(TEXT, TEXT, TEXT[]) FROM PUBLIC;

-- The agent's database principal is mapped to tizert_kg_reader: no table privilege at all.
GRANT USAGE ON SCHEMA kg TO tizert_kg_reader;
GRANT EXECUTE ON FUNCTION kg.active_release(TEXT), kg.resolve(TEXT, TEXT, TEXT[], INTEGER),
    kg.lookup(TEXT, TEXT, TEXT[], TEXT[], INTEGER), kg.entity(TEXT, TEXT, TEXT[]),
    kg.neighbors(TEXT, TEXT, TEXT[], TEXT, TEXT, INTEGER), kg.lineage(TEXT, TEXT, TEXT[]),
    kg.relations_between(TEXT, TEXT, TEXT, TEXT[]), kg.provenance(TEXT, TEXT[], TEXT[]),
    kg.legend(TEXT, TEXT, TEXT[]) TO tizert_kg_reader;

GRANT SELECT, INSERT ON kg.alias_set, kg.alias TO tizert_kg_writer;
GRANT SELECT ON kg.alias_set, kg.alias TO tizert_kg_release;
