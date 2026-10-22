-- find_documents also returns each document's content_hash: the agent (and the golden-set
-- retrieval evaluation) steers Search to the graph's documents with search.in(content_hash).
DROP FUNCTION crv_kg.find_documents(TEXT, TEXT[], TEXT[], TEXT[], TEXT[], INTEGER, INTEGER,
    TEXT, INTEGER);

CREATE FUNCTION crv_kg.find_documents(p_release TEXT, p_scopes TEXT[],
    p_entities TEXT[] DEFAULT NULL, p_genres TEXT[] DEFAULT NULL,
    p_collections TEXT[] DEFAULT NULL, p_from_year INTEGER DEFAULT NULL,
    p_to_year INTEGER DEFAULT NULL, p_title TEXT DEFAULT NULL, p_limit INTEGER DEFAULT 50)
    RETURNS TABLE (iri TEXT, title TEXT, genre TEXT, date TEXT, collection TEXT,
        report_codes JSONB, linked JSONB, source_paths JSONB, status TEXT, content_hash TEXT)
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
        docs.properties ->> 'status',
        docs.properties ->> 'content_hash'
    FROM docs
    ORDER BY docs.properties ->> 'date' DESC NULLS LAST, docs.iri
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 200)
$$;

REVOKE ALL ON FUNCTION crv_kg.find_documents(TEXT, TEXT[], TEXT[], TEXT[], TEXT[], INTEGER,
    INTEGER, TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION crv_kg.find_documents(TEXT, TEXT[], TEXT[], TEXT[], TEXT[], INTEGER,
    INTEGER, TEXT, INTEGER) TO crv_kg_reader;
