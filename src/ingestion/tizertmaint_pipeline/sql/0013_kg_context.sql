-- One-call grounding context for the agent (each call from AKS pays a ~36 ms round trip).
-- Built only from the 0012 read functions, so release pinning and per-hop scope filtering
-- are inherited unchanged.

-- A visible entity with its parent equipment, areas (of the parent for an instrument) and
-- their process groups, first p_children instrument tags with the total, referenced
-- documents and, for an instrument, the legend meanings of its tag prefix. NULL if invisible.
CREATE FUNCTION kg.entity_context(p_release TEXT, p_iri TEXT, p_scopes TEXT[],
        p_children INTEGER DEFAULT 12)
    RETURNS JSONB
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    WITH card AS (
        SELECT * FROM kg.entity(p_release, p_iri, p_scopes)
    ),
    parent AS (
        SELECT n.iri, n.identifier, n.name, n.status, n.assertion_iris
        FROM card, kg.neighbors(p_release, p_iri, p_scopes, 'TAG_OF', 'out', 5) AS n
        ORDER BY n.identifier COLLATE "C"
        LIMIT 1
    ),
    areas AS (
        SELECT n.iri, n.identifier, n.name, n.status, n.assertion_iris
        FROM card, kg.neighbors(p_release, coalesce((SELECT parent.iri FROM parent), p_iri),
            p_scopes, 'REPORTED_IN_AREA', 'out', 50) AS n
    ),
    children AS (
        SELECT n.iri, n.identifier, n.name, n.total
        FROM card, kg.neighbors(p_release, p_iri, p_scopes, 'TAG_OF', 'in',
            least(greatest(coalesce(p_children, 12), 1), 50)) AS n
    ),
    documents AS (
        SELECT n.identifier, n.status, n.assertion_iris
        FROM card, kg.neighbors(p_release, p_iri, p_scopes, 'REFERENCES_DOCUMENT', 'out', 25) AS n
    ),
    legend AS (
        SELECT l.name, l.class, l.signal, l.status
        FROM card, kg.legend(p_release, card.attributes ->> 'Tag Prefix', p_scopes) AS l
        WHERE card.kind = 'InstrumentTag'
    )
    SELECT jsonb_build_object(
        'iri', card.iri, 'kind', card.kind, 'identifier', card.identifier, 'name', card.name,
        'status', card.status, 'attributes', card.attributes, 'sources', card.sources,
        'parent', (
            SELECT jsonb_build_object('iri', p.iri, 'identifier', p.identifier, 'name', p.name,
                'status', p.status, 'assertion_iris', to_jsonb(p.assertion_iris))
            FROM parent AS p
        ),
        'areas', coalesce((
            SELECT jsonb_agg(jsonb_build_object('iri', a.iri, 'identifier', a.identifier,
                    'name', a.name, 'status', a.status, 'assertion_iris', to_jsonb(a.assertion_iris),
                    'groups', coalesce((
                        SELECT jsonb_agg(jsonb_build_object('identifier', g.identifier,
                                'name', g.name) ORDER BY g.identifier COLLATE "C")
                        FROM kg.neighbors(p_release, a.iri, p_scopes, 'IN_PROCESS_GROUP', 'out', 5)
                            AS g), '[]'::jsonb))
                ORDER BY a.identifier COLLATE "C")
            FROM areas AS a), '[]'::jsonb),
        'children', jsonb_build_object(
            'total', coalesce((SELECT max(c.total) FROM children AS c), 0),
            'items', coalesce((
                SELECT jsonb_agg(jsonb_build_object('iri', c.iri, 'identifier', c.identifier,
                        'name', c.name) ORDER BY c.identifier COLLATE "C")
                FROM children AS c), '[]'::jsonb)),
        'documents', coalesce((
            SELECT jsonb_agg(jsonb_build_object('identifier', d.identifier, 'status', d.status,
                    'assertion_iris', to_jsonb(d.assertion_iris)) ORDER BY d.identifier COLLATE "C")
            FROM documents AS d), '[]'::jsonb),
        'legend', coalesce((
            SELECT jsonb_agg(jsonb_build_object('name', l.name, 'class', l.class,
                    'signal', l.signal, 'status', l.status) ORDER BY l.name COLLATE "C")
            FROM legend AS l), '[]'::jsonb))
    FROM card
$$;

-- kg.resolve plus the entity context of every visible match, in rank order.
CREATE FUNCTION kg.context(p_release TEXT, p_text TEXT, p_scopes TEXT[],
        p_limit INTEGER DEFAULT 5, p_children INTEGER DEFAULT 12)
    RETURNS JSONB
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT jsonb_build_object(
        'release_id', p_release,
        'ambiguous', coalesce(bool_or(f.ambiguous), false),
        'matches', coalesce(jsonb_agg(jsonb_build_object(
                'rank', f.rank, 'mention', f.mention, 'method', f.method, 'score', f.score,
                'iri', f.iri, 'kind', f.kind, 'identifier', f.identifier, 'name', f.name,
                'status', f.status, 'alias_label', f.alias_label, 'alias_note', f.alias_note,
                'context', CASE WHEN f.iri IS NOT NULL
                    THEN kg.entity_context(p_release, f.iri, p_scopes, p_children) END)
            ORDER BY f.rank), '[]'::jsonb))
    FROM kg.resolve(p_release, p_text, p_scopes, p_limit) AS f
$$;

REVOKE ALL ON FUNCTION kg.entity_context(TEXT, TEXT, TEXT[], INTEGER),
    kg.context(TEXT, TEXT, TEXT[], INTEGER, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kg.entity_context(TEXT, TEXT, TEXT[], INTEGER),
    kg.context(TEXT, TEXT, TEXT[], INTEGER, INTEGER) TO tizert_kg_reader;
