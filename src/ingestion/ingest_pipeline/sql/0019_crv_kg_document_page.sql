-- Stable document enumeration for the research backend. Existing read contracts are unchanged.
CREATE FUNCTION crv_kg.list_documents(
    p_release TEXT, p_scopes TEXT[], p_entities TEXT[] DEFAULT NULL,
    p_after TEXT DEFAULT NULL, p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
    iri TEXT, title TEXT, genre TEXT, date TEXT, collection TEXT, status TEXT,
    content_hash TEXT, source_paths JSONB, total_count BIGINT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    WITH visible AS MATERIALIZED (
        SELECT d.iri, d.properties
        FROM crv_kg.vertex_projection AS d
        WHERE d.release_id = p_release AND d.label = 'Document'
            AND d.scope_keys && p_scopes
            AND (p_entities IS NULL OR EXISTS (
                SELECT 1 FROM crv_kg.edge_projection AS e
                JOIN crv_kg.vertex_projection AS t
                    ON t.release_id = p_release AND t.iri = e.end_iri
                    AND t.scope_keys && p_scopes
                WHERE e.release_id = p_release AND e.start_iri = d.iri
                    AND e.end_iri = ANY (p_entities) AND e.scope_keys && p_scopes
            ))
    )
    SELECT d.iri, d.properties ->> 'title', d.properties ->> 'genre',
        d.properties ->> 'date', d.properties ->> 'collection',
        d.properties ->> 'status', d.properties ->> 'content_hash',
        coalesce(d.properties -> 'source_paths', '[]'::jsonb),
        (SELECT count(*) FROM visible)
    FROM visible AS d
    WHERE p_after IS NULL OR d.iri > p_after
    ORDER BY d.iri
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201)
$$;

REVOKE ALL ON FUNCTION crv_kg.list_documents(TEXT, TEXT[], TEXT[], TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION crv_kg.list_documents(TEXT, TEXT[], TEXT[], TEXT, INTEGER)
    TO crv_kg_reader;
