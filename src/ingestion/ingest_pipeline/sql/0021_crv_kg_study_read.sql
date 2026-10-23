-- KG-2 pilot read API (design §6-7): studies and observations frozen into a release, filtered by
-- the caller's scopes on the document vertex and on the evidence row, optionally by entities
-- linked to the reporting document (sites, projects, report codes). Results carry document
-- hashes, pages and verbatim quotes so the agent fetches and cites the source text itself; a
-- graph record is never an answer on its own. Same conventions as 0017: release-pinned,
-- SECURITY DEFINER, EXECUTE only for crv_kg_reader.
CREATE FUNCTION crv_kg.find_studies(
    p_release TEXT, p_scopes TEXT[], p_entities TEXT[] DEFAULT NULL,
    p_scales TEXT[] DEFAULT NULL, p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
    study_iri TEXT, label TEXT, scale TEXT, period TEXT, facility TEXT, status TEXT,
    document_iri TEXT, content_hash TEXT, source_paths JSONB, pages INTEGER[])
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT s.iri, s.properties ->> 'name', s.properties ->> 'scale',
        s.properties ->> 'period', s.properties ->> 'facility', s.properties ->> 'status',
        d.iri, d.properties ->> 'content_hash', coalesce(d.properties -> 'source_paths', '[]'),
        ARRAY(
            SELECT DISTINCT (ev.locator ->> 'page')::INTEGER
            FROM crv_kg.entity_evidence AS link
            JOIN crv_kg.evidence AS ev ON ev.iri = link.evidence_iri
            WHERE link.entity_iri = s.iri AND ev.scope_key = ANY (p_scopes)
                AND ev.locator ? 'page'
            ORDER BY 1
        )
    FROM crv_kg.vertex_projection AS s
    JOIN crv_kg.edge_projection AS r
        ON r.release_id = p_release AND r.label = 'REPORTS' AND r.end_iri = s.iri
        AND r.scope_keys && p_scopes
    JOIN crv_kg.vertex_projection AS d
        ON d.release_id = p_release AND d.iri = r.start_iri AND d.label = 'Document'
        AND d.scope_keys && p_scopes
    WHERE s.release_id = p_release AND s.label = 'Study' AND s.scope_keys && p_scopes
        AND (p_scales IS NULL OR s.properties ->> 'scale' = ANY (p_scales))
        AND (p_entities IS NULL OR EXISTS (
            SELECT 1 FROM crv_kg.edge_projection AS e
            WHERE e.release_id = p_release AND e.start_iri = d.iri
                AND e.end_iri = ANY (p_entities) AND e.scope_keys && p_scopes
        ))
    ORDER BY s.properties ->> 'period' NULLS LAST, s.iri
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

CREATE FUNCTION crv_kg.get_observations(
    p_release TEXT, p_scopes TEXT[], p_entities TEXT[] DEFAULT NULL,
    p_properties TEXT[] DEFAULT NULL, p_limit INTEGER DEFAULT 200)
RETURNS TABLE (
    observation_iri TEXT, property TEXT, element TEXT, stream TEXT, basis TEXT,
    value_text TEXT, value_num NUMERIC, unit TEXT, qualifier TEXT, method TEXT, status TEXT,
    subject_text TEXT, study_iri TEXT, study_label TEXT, scale TEXT, period TEXT,
    document_iri TEXT, content_hash TEXT, source_paths JSONB, page INTEGER, quote TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT o.iri, o.property, o.element, o.stream, o.basis, o.value_text, o.value_num, o.unit,
        o.qualifier, o.method, member.status, o.subject_text, o.study_iri,
        s.properties ->> 'name', s.properties ->> 'scale', s.properties ->> 'period',
        d.iri, d.properties ->> 'content_hash', coalesce(d.properties -> 'source_paths', '[]'),
        (ev.locator ->> 'page')::INTEGER, ev.locator ->> 'quote'
    FROM crv_kg.release_observation AS member
    JOIN crv_kg.observation AS o ON o.iri = member.observation_iri
    JOIN crv_kg.evidence AS ev ON ev.iri = o.evidence_iri AND ev.scope_key = ANY (p_scopes)
    JOIN crv_kg.vertex_projection AS d
        ON d.release_id = p_release AND d.iri = o.document_iri AND d.scope_keys && p_scopes
    LEFT JOIN crv_kg.vertex_projection AS s
        ON s.release_id = p_release AND s.iri = o.study_iri AND s.scope_keys && p_scopes
    WHERE member.release_id = p_release
        AND (p_properties IS NULL OR o.property = ANY (p_properties))
        AND (p_entities IS NULL OR EXISTS (
            SELECT 1 FROM crv_kg.edge_projection AS e
            WHERE e.release_id = p_release AND e.start_iri = d.iri
                AND e.end_iri = ANY (p_entities) AND e.scope_keys && p_scopes
        ))
    ORDER BY o.property, d.iri, (ev.locator ->> 'page')::INTEGER, o.iri
    LIMIT least(greatest(coalesce(p_limit, 200), 1), 500)
$$;

REVOKE ALL ON FUNCTION crv_kg.find_studies(TEXT, TEXT[], TEXT[], TEXT[], INTEGER),
    crv_kg.get_observations(TEXT, TEXT[], TEXT[], TEXT[], INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION crv_kg.find_studies(TEXT, TEXT[], TEXT[], TEXT[], INTEGER),
    crv_kg.get_observations(TEXT, TEXT[], TEXT[], TEXT[], INTEGER) TO crv_kg_reader;
