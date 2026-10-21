-- Release-pinned, read-only CRV knowledge-graph API for the agent (design §7). Each function
-- reads one immutable release, applies the caller's scope keys to every vertex, edge and
-- evidence row before ranking and limits, and runs as its owner: crv_kg_reader needs EXECUTE
-- only. Invisible equals unknown: nothing reports whether an out-of-scope entity exists.

-- Lower-case ASCII words separated by single spaces (as domain/crv_kg/vocab.py fold()).
CREATE FUNCTION crv_kg.fold(value TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN btrim(regexp_replace(
        lower(regexp_replace(normalize(value, NFKD), '[^\x01-\x7f]', '', 'g')),
        '[^a-z0-9]+', ' ', 'g'));

-- The release an environment currently serves.
CREATE FUNCTION crv_kg.active_release(p_environment TEXT)
    RETURNS TABLE (release_id TEXT, graph_name TEXT, activated_at TIMESTAMPTZ)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT active.release_id, active.graph_name, active.activated_at
    FROM crv_kg.release_active AS active
    WHERE active.environment = p_environment
$$;

-- Sites, organisations, projects and report codes named in free text: whole-word alias matches
-- (Ouanssimi -> Ouansimi, MAA, JBL) and report codes written with any separators. Longest
-- match first; candidates (unreviewed gazetteer entries) are returned with their status.
CREATE FUNCTION crv_kg.resolve(p_release TEXT, p_text TEXT, p_scopes TEXT[],
    p_limit INTEGER DEFAULT 10)
    RETURNS TABLE (rank INTEGER, iri TEXT, kind TEXT, name TEXT, status TEXT, matched TEXT,
        method TEXT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH params AS (
        SELECT ' ' || crv_kg.fold(coalesce(p_text, '')) || ' ' AS padded,
            replace(crv_kg.fold(coalesce(p_text, '')), ' ', '') AS compact,
            least(greatest(coalesce(p_limit, 10), 1), 50) AS cap
    ),
    hits AS (
        SELECT DISTINCT ON (v.iri) v.iri, v.label AS kind, v.properties ->> 'name' AS name,
            v.properties ->> 'status' AS status, k.key AS matched,
            CASE v.label WHEN 'ReportCode' THEN 'code' ELSE 'alias' END AS method
        FROM crv_kg.vertex_projection AS v
        CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(v.properties -> 'keys', '[]'::jsonb))
            AS k(key)
        CROSS JOIN params
        WHERE v.release_id = p_release
            AND v.scope_keys && p_scopes
            AND v.label IN ('Site', 'Organisation', 'Project', 'ReportCode')
            AND k.key <> ''
            AND CASE v.label
                WHEN 'ReportCode' THEN length(k.key) >= 8
                    AND strpos(params.compact, k.key) > 0
                ELSE strpos(params.padded, ' ' || k.key || ' ') > 0
            END
        ORDER BY v.iri, length(k.key) DESC
    )
    SELECT (row_number() OVER (ORDER BY length(h.matched) DESC, h.kind, h.iri))::integer,
        h.iri, h.kind, h.name, h.status, h.matched, h.method
    FROM hits AS h
    ORDER BY 1
    LIMIT (SELECT cap FROM params)
$$;

-- One visible vertex with its projected properties and up to 20 visible evidence locators
-- recorded before the release was built.
CREATE FUNCTION crv_kg.entity(p_release TEXT, p_iri TEXT, p_scopes TEXT[])
    RETURNS TABLE (iri TEXT, kind TEXT, name TEXT, status TEXT, properties JSONB,
        evidence JSONB)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT v.iri, v.label, v.properties ->> 'name', v.properties ->> 'status',
        v.properties - 'keys' - 'release_id',
        coalesce((
            SELECT jsonb_agg(jsonb_build_object('evidence_iri', e.iri,
                    'locator_type', e.locator_type, 'locator', e.locator,
                    'source_name', e.source_name, 'source_hash', e.source_hash)
                ORDER BY e.locator_type, e.iri)
            FROM (
                SELECT ev.iri, ev.locator_type, ev.locator, ev.source_name, ev.source_hash
                FROM crv_kg.entity_evidence AS link
                JOIN crv_kg.evidence AS ev ON ev.iri = link.evidence_iri
                JOIN crv_kg.release AS rel ON rel.release_id = p_release
                WHERE link.entity_iri = v.iri AND ev.scope_key = ANY (p_scopes)
                    AND ev.recorded_at <= rel.created_at
                ORDER BY ev.locator_type, ev.iri
                LIMIT 20
            ) AS e
        ), '[]'::jsonb)
    FROM crv_kg.vertex_projection AS v
    WHERE v.release_id = p_release AND v.iri = p_iri AND v.scope_keys && p_scopes
$$;

-- One-hop neighbourhood of a visible vertex, at most p_limit per (relation, direction); the
-- start vertex, each edge and each neighbour must share a scope with the caller.
CREATE FUNCTION crv_kg.neighbors(p_release TEXT, p_iri TEXT, p_scopes TEXT[],
    p_label TEXT DEFAULT NULL, p_direction TEXT DEFAULT NULL, p_limit INTEGER DEFAULT 25)
    RETURNS TABLE (relation TEXT, direction TEXT, iri TEXT, kind TEXT, name TEXT,
        edge_status TEXT, methods JSONB)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH start AS (
        SELECT v.iri FROM crv_kg.vertex_projection AS v
        WHERE v.release_id = p_release AND v.iri = p_iri AND v.scope_keys && p_scopes
    ),
    candidates AS (
        SELECT e.label, 'out'::text AS direction, e.end_iri AS neighbor, e.properties
        FROM crv_kg.edge_projection AS e JOIN start ON e.start_iri = start.iri
        WHERE e.release_id = p_release AND e.scope_keys && p_scopes
        UNION ALL
        SELECT e.label, 'in', e.start_iri, e.properties
        FROM crv_kg.edge_projection AS e JOIN start ON e.end_iri = start.iri
        WHERE e.release_id = p_release AND e.scope_keys && p_scopes
    ),
    visible AS (
        SELECT c.label, c.direction, n.iri, n.label AS kind, n.properties ->> 'name' AS name,
            c.properties ->> 'status' AS edge_status, c.properties -> 'methods' AS methods,
            row_number() OVER (PARTITION BY c.label, c.direction
                ORDER BY n.properties ->> 'name', n.iri) AS position
        FROM candidates AS c
        JOIN crv_kg.vertex_projection AS n
            ON n.release_id = p_release AND n.iri = c.neighbor AND n.scope_keys && p_scopes
        WHERE (p_label IS NULL OR c.label = p_label)
            AND (p_direction IS NULL OR c.direction = p_direction)
    )
    SELECT label, direction, iri, kind, name, edge_status, methods
    FROM visible
    WHERE position <= least(greatest(coalesce(p_limit, 25), 1), 100)
    ORDER BY label, direction, position
$$;

-- Documents filtered by linked entities (sites, organisations, projects, report codes: any
-- match), genre, collection, year range and title words; newest first.
CREATE FUNCTION crv_kg.find_documents(p_release TEXT, p_scopes TEXT[],
    p_entities TEXT[] DEFAULT NULL, p_genres TEXT[] DEFAULT NULL,
    p_collections TEXT[] DEFAULT NULL, p_from_year INTEGER DEFAULT NULL,
    p_to_year INTEGER DEFAULT NULL, p_title TEXT DEFAULT NULL, p_limit INTEGER DEFAULT 50)
    RETURNS TABLE (iri TEXT, title TEXT, genre TEXT, date TEXT, collection TEXT,
        report_codes JSONB, linked JSONB, source_paths JSONB, status TEXT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH docs AS (
        SELECT d.iri, d.properties
        FROM crv_kg.vertex_projection AS d
        WHERE d.release_id = p_release AND d.label = 'Document' AND d.scope_keys && p_scopes
            AND (p_genres IS NULL OR d.properties ->> 'genre' = ANY (p_genres))
            AND (p_collections IS NULL OR d.properties ->> 'collection' = ANY (p_collections))
            AND (p_from_year IS NULL OR (d.properties ->> 'year')::integer >= p_from_year)
            AND (p_to_year IS NULL OR (d.properties ->> 'year')::integer <= p_to_year)
            AND (p_title IS NULL OR ' ' || crv_kg.fold(coalesce(d.properties ->> 'title', '')
                    || ' ' || coalesce(d.properties ->> 'file_title', '')) || ' '
                LIKE '% ' || crv_kg.fold(p_title) || ' %')
            AND (p_entities IS NULL OR EXISTS (
                SELECT 1 FROM crv_kg.edge_projection AS e
                JOIN crv_kg.vertex_projection AS t
                    ON t.release_id = p_release AND t.iri = e.end_iri
                    AND t.scope_keys && p_scopes
                WHERE e.release_id = p_release AND e.start_iri = d.iri
                    AND e.end_iri = ANY (p_entities) AND e.scope_keys && p_scopes))
    )
    SELECT docs.iri, docs.properties ->> 'title', docs.properties ->> 'genre',
        docs.properties ->> 'date', docs.properties ->> 'collection',
        coalesce(docs.properties -> 'report_codes', '[]'::jsonb),
        coalesce((
            SELECT jsonb_agg(jsonb_build_object('relation', e.label, 'iri', t.iri,
                    'kind', t.label, 'name', t.properties ->> 'name',
                    'status', e.properties ->> 'status') ORDER BY e.label, t.iri)
            FROM crv_kg.edge_projection AS e
            JOIN crv_kg.vertex_projection AS t
                ON t.release_id = p_release AND t.iri = e.end_iri AND t.scope_keys && p_scopes
            WHERE e.release_id = p_release AND e.start_iri = docs.iri
                AND e.scope_keys && p_scopes
                AND e.label IN ('ABOUT_SITE', 'CONCERNS', 'ISSUED_BY', 'ATTRIBUTED_TO',
                    'ABOUT_PROJECT')
        ), '[]'::jsonb),
        coalesce(docs.properties -> 'source_paths', '[]'::jsonb),
        docs.properties ->> 'status'
    FROM docs
    ORDER BY docs.properties ->> 'date' DESC NULLS LAST, docs.iri
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

-- Lineage of a visible document: supersedes / duplicate edges both ways, other members of
-- its version family, and the set (folder) it is a chapter or annex of, with the other parts.
CREATE FUNCTION crv_kg.document_lineage(p_release TEXT, p_iri TEXT, p_scopes TEXT[])
    RETURNS TABLE (relation TEXT, direction TEXT, iri TEXT, kind TEXT, name TEXT, date TEXT,
        edge_status TEXT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH start AS (
        SELECT v.iri FROM crv_kg.vertex_projection AS v
        WHERE v.release_id = p_release AND v.iri = p_iri AND v.label = 'Document'
            AND v.scope_keys && p_scopes
    ),
    edges AS (
        SELECT e.* FROM crv_kg.edge_projection AS e
        WHERE e.release_id = p_release AND e.scope_keys && p_scopes
    ),
    links AS (
        SELECT e.label AS relation, 'out' AS direction, e.end_iri AS other,
            e.properties ->> 'status' AS edge_status
        FROM edges AS e JOIN start ON e.start_iri = start.iri
        WHERE e.label IN ('SUPERSEDES', 'DUPLICATE_OF', 'PART_OF', 'MEMBER_OF')
        UNION ALL
        SELECT e.label, 'in', e.start_iri, e.properties ->> 'status'
        FROM edges AS e JOIN start ON e.end_iri = start.iri
        WHERE e.label IN ('SUPERSEDES', 'DUPLICATE_OF')
        UNION ALL
        SELECT CASE mine.label WHEN 'MEMBER_OF' THEN 'SAME_FAMILY' ELSE 'SAME_SET' END,
            'sibling', other.start_iri, other.properties ->> 'status'
        FROM edges AS mine
        JOIN start ON mine.start_iri = start.iri
        JOIN edges AS other ON other.label = mine.label AND other.end_iri = mine.end_iri
            AND other.start_iri <> start.iri
        WHERE mine.label IN ('MEMBER_OF', 'PART_OF')
    )
    SELECT l.relation, l.direction, v.iri, v.label, v.properties ->> 'name',
        v.properties ->> 'date', l.edge_status
    FROM links AS l
    JOIN crv_kg.vertex_projection AS v
        ON v.release_id = p_release AND v.iri = l.other AND v.scope_keys && p_scopes
    ORDER BY l.relation, l.direction, v.properties ->> 'date' DESC NULLS LAST, v.iri
    LIMIT 100
$$;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crv_kg_reader') THEN
        CREATE ROLE crv_kg_reader NOLOGIN;
    END IF;
END
$roles$;

REVOKE ALL ON FUNCTION crv_kg.active_release(TEXT),
    crv_kg.resolve(TEXT, TEXT, TEXT[], INTEGER),
    crv_kg.entity(TEXT, TEXT, TEXT[]),
    crv_kg.neighbors(TEXT, TEXT, TEXT[], TEXT, TEXT, INTEGER),
    crv_kg.find_documents(TEXT, TEXT[], TEXT[], TEXT[], TEXT[], INTEGER, INTEGER, TEXT, INTEGER),
    crv_kg.document_lineage(TEXT, TEXT, TEXT[]) FROM PUBLIC;

-- The agent's database principal is mapped to crv_kg_reader: no table privilege at all.
GRANT USAGE ON SCHEMA crv_kg TO crv_kg_reader;
GRANT EXECUTE ON FUNCTION crv_kg.fold(TEXT), crv_kg.active_release(TEXT),
    crv_kg.resolve(TEXT, TEXT, TEXT[], INTEGER),
    crv_kg.entity(TEXT, TEXT, TEXT[]),
    crv_kg.neighbors(TEXT, TEXT, TEXT[], TEXT, TEXT, INTEGER),
    crv_kg.find_documents(TEXT, TEXT[], TEXT[], TEXT[], TEXT[], INTEGER, INTEGER, TEXT, INTEGER),
    crv_kg.document_lineage(TEXT, TEXT, TEXT[]) TO crv_kg_reader;
