-- 0025_crv_retrieval_bm25: rank the keyword leg of crv_retrieval.search with BM25.
-- ts_rank weighs every query word alike and favours the title, which every chunk of a document
-- repeats, so a few documents filled the leg. On 150 business questions its top 10 held 0.39 of
-- the answer anchors against 0.69 for the Azure AI Search keyword query; BM25 over the same
-- tsvector (term frequency per position weight, inverse document frequency, length
-- normalisation) held 0.68, and the fused search matched the Azure hybrid (0.72 vs 0.73).
-- Document frequencies come from lexeme_stats, which refresh_keyword_stats rebuilds (the
-- retrieval-index step runs it after every backfill); a release without statistics keeps ts_rank.

-- A function may only SET hnsw.* once pgvector is loaded in the session (see 0024).
SELECT '[1]'::vector;

CREATE TABLE crv_retrieval.release_stats (
    release_id TEXT PRIMARY KEY,
    chunks BIGINT NOT NULL CHECK (chunks > 0),
    average_length DOUBLE PRECISION NOT NULL CHECK (average_length > 0),
    refreshed_at TIMESTAMPTZ NOT NULL
);

-- Lexemes of at least two chunks of a release: any other lexeme is in one chunk at most.
CREATE TABLE crv_retrieval.lexeme_stats (
    release_id TEXT NOT NULL,
    lexeme TEXT NOT NULL,
    chunks INTEGER NOT NULL CHECK (chunks >= 2),
    PRIMARY KEY (release_id, lexeme)
);

CREATE FUNCTION crv_retrieval.refresh_keyword_stats() RETURNS JSONB
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    target TEXT;
BEGIN
    DELETE FROM crv_retrieval.lexeme_stats;
    DELETE FROM crv_retrieval.release_stats;
    FOR target IN SELECT DISTINCT c.release_id FROM crv_retrieval.chunk AS c LOOP
        INSERT INTO crv_retrieval.release_stats (release_id, chunks, average_length, refreshed_at)
        SELECT target, count(*), avg(length(c.search_text)), now()
        FROM crv_retrieval.chunk AS c WHERE c.release_id = target;
        INSERT INTO crv_retrieval.lexeme_stats (release_id, lexeme, chunks)
        SELECT target, s.word, s.ndoc
        FROM ts_stat(format(
            'SELECT search_text FROM crv_retrieval.chunk WHERE release_id = %L', target)) AS s
        WHERE s.ndoc >= 2;
    END LOOP;
    RETURN coalesce((
        SELECT jsonb_object_agg(r.release_id, jsonb_build_object(
            'chunks', r.chunks, 'average_length', round(r.average_length::numeric, 2),
            'lexemes', (SELECT count(*) FROM crv_retrieval.lexeme_stats AS l
                        WHERE l.release_id = r.release_id)))
        FROM crv_retrieval.release_stats AS r), '{}');
END
$$;

-- The keyword leg: chunks matching the rarer query words (match_terms, 0024), ranked by BM25
-- over every query word (k1 = 1.2, b = 0.75; paths count half a title or content occurrence).
CREATE FUNCTION crv_retrieval.keyword_leg(
    p_release TEXT, p_text TEXT, p_hashes TEXT[], p_depth INTEGER)
    RETURNS TEXT[]
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    SET enable_seqscan = off
    SET enable_sort = off
AS $$
#variable_conflict use_column
DECLARE
    hashes TEXT[] := coalesce(p_hashes, '{}');
    scoped BOOLEAN := cardinality(coalesce(p_hashes, '{}')) > 0;
    terms tsquery := crv_retrieval.query_terms(coalesce(p_text, ''));
    matching tsquery;
    stats crv_retrieval.release_stats;
BEGIN
    IF terms IS NULL THEN
        RETURN '{}';
    END IF;
    -- A scope is a few documents: every word may match.
    matching := CASE WHEN scoped THEN terms
        ELSE crv_retrieval.match_terms(coalesce(p_text, '')) END;
    SELECT * INTO stats FROM crv_retrieval.release_stats AS s WHERE s.release_id = p_release;
    IF stats.release_id IS NULL THEN
        RETURN ARRAY(
            SELECT c.projection_id FROM crv_retrieval.chunk AS c
            WHERE c.release_id = p_release AND c.search_text @@ matching
                AND (NOT scoped OR c.content_hash = ANY (hashes))
            ORDER BY ts_rank(c.search_text, terms, 1) DESC, c.projection_id
            LIMIT p_depth);
    END IF;
    RETURN ARRAY(
        WITH word AS (
            SELECT w.lexeme, ln(1 + (stats.chunks - coalesce(l.chunks, 1) + 0.5)
                / (coalesce(l.chunks, 1) + 0.5)) AS idf
            FROM unnest(crv_retrieval.query_lexemes(coalesce(p_text, ''))) AS w(lexeme)
            LEFT JOIN crv_retrieval.lexeme_stats AS l
                ON l.release_id = p_release AND l.lexeme = w.lexeme
        ), candidate AS MATERIALIZED (
            SELECT c.projection_id, c.search_text, length(c.search_text) AS size
            FROM crv_retrieval.chunk AS c
            WHERE c.release_id = p_release AND c.search_text @@ matching
                AND (NOT scoped OR c.content_hash = ANY (hashes))
        )
        SELECT candidate.projection_id
        FROM candidate
        CROSS JOIN LATERAL (
            SELECT sum(word.idf * f.tf * 2.2 / (f.tf + 1.2 * (0.25
                + 0.75 * candidate.size / stats.average_length))) AS score
            FROM unnest(candidate.search_text) AS u(lexeme, positions, weights)
            JOIN word ON word.lexeme = u.lexeme
            CROSS JOIN LATERAL (
                SELECT coalesce(sum(CASE w.weight WHEN 'B' THEN 0.5 ELSE 1.0 END), 1) AS tf
                FROM unnest(u.weights) AS w(weight)
            ) AS f
        ) AS bm25
        ORDER BY bm25.score DESC NULLS LAST, candidate.projection_id
        LIMIT p_depth);
END
$$;

CREATE OR REPLACE FUNCTION crv_retrieval.search(
    p_release TEXT, p_text TEXT, p_hashes TEXT[], p_model TEXT, p_vector REAL[],
    p_top INTEGER DEFAULT 50)
    RETURNS TABLE (projection_id TEXT, release_id TEXT, chunk_id TEXT, content_hash TEXT,
        title TEXT, content TEXT, source_uri TEXT, source_path TEXT, document_code TEXT,
        original_page_numbers INTEGER[], source_locators TEXT[], original_source_uris TEXT[],
        is_generated BOOLEAN, document_class TEXT, citation_basis TEXT, extraction_route TEXT,
        original_content_hashes TEXT[], score DOUBLE PRECISION)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    SET hnsw.ef_search = 500
    SET hnsw.iterative_scan = relaxed_order
    SET enable_seqscan = off
    SET enable_sort = off
AS $$
#variable_conflict use_column
DECLARE
    depth CONSTANT INTEGER := 50;
    hashes TEXT[] := coalesce(p_hashes, '{}');
    scoped BOOLEAN := cardinality(coalesce(p_hashes, '{}')) > 0;
    terms tsquery := crv_retrieval.query_terms(coalesce(p_text, ''));
    keys TEXT[] := crv_retrieval.query_identifiers(coalesce(p_text, ''));
    lexical TEXT[] := '{}';
    identified TEXT[] := '{}';
    nearest TEXT[] := '{}';
    te3 vector(3072);
    m3 vector(1024);
BEGIN
    IF p_top IS NULL OR p_top < 1 OR p_top > 100 THEN
        RAISE EXCEPTION 'p_top must be between 1 and 100';
    END IF;
    IF cardinality(hashes) > 1000
        OR EXISTS (SELECT 1 FROM unnest(hashes) AS h WHERE h !~ '^[0-9a-f]{64}$') THEN
        RAISE EXCEPTION 'Invalid content hash filter';
    END IF;

    lexical := crv_retrieval.keyword_leg(p_release, p_text, hashes, depth);

    IF cardinality(keys) > 0 THEN
        identified := ARRAY(
            SELECT c.projection_id FROM crv_retrieval.chunk AS c
            WHERE c.release_id = p_release AND c.identifier_keys && keys
                AND (NOT scoped OR c.content_hash = ANY (hashes))
            ORDER BY cardinality(ARRAY(
                    SELECT unnest(c.identifier_keys) INTERSECT SELECT unnest(keys))) DESC,
                CASE WHEN terms IS NULL THEN 0 ELSE ts_rank(c.search_text, terms, 1) END DESC,
                c.projection_id
            LIMIT depth);
    END IF;

    IF p_vector IS NOT NULL AND p_model = 'te3' THEN
        IF cardinality(p_vector) <> 3072 THEN
            RAISE EXCEPTION 'te3 query vectors have 3072 dimensions';
        END IF;
        te3 := p_vector::vector(3072);
        IF scoped THEN
            nearest := ARRAY(
                WITH candidate AS MATERIALIZED (
                    SELECT c.projection_id, c.embedding_te3 FROM crv_retrieval.chunk AS c
                    WHERE c.release_id = p_release AND c.content_hash = ANY (hashes)
                        AND c.embedding_te3 IS NOT NULL)
                SELECT candidate.projection_id FROM candidate
                ORDER BY candidate.embedding_te3 <=> te3, candidate.projection_id
                LIMIT depth);
        ELSE
            nearest := ARRAY(
                WITH candidate AS MATERIALIZED (
                    SELECT c.projection_id, c.embedding_te3 FROM crv_retrieval.chunk AS c
                    WHERE c.release_id = p_release
                    ORDER BY c.embedding_te3::halfvec(3072) <=> te3::halfvec(3072)
                    LIMIT 2 * depth)
                SELECT candidate.projection_id FROM candidate
                WHERE candidate.embedding_te3 IS NOT NULL
                ORDER BY candidate.embedding_te3 <=> te3, candidate.projection_id
                LIMIT depth);
        END IF;
    ELSIF p_vector IS NOT NULL AND p_model = 'm3' THEN
        IF cardinality(p_vector) <> 1024 THEN
            RAISE EXCEPTION 'm3 query vectors have 1024 dimensions';
        END IF;
        m3 := p_vector::vector(1024);
        IF scoped THEN
            nearest := ARRAY(
                WITH candidate AS MATERIALIZED (
                    SELECT c.projection_id, c.embedding_m3 FROM crv_retrieval.chunk AS c
                    WHERE c.release_id = p_release AND c.content_hash = ANY (hashes)
                        AND c.embedding_m3 IS NOT NULL)
                SELECT candidate.projection_id FROM candidate
                ORDER BY candidate.embedding_m3 <=> m3, candidate.projection_id
                LIMIT depth);
        ELSE
            nearest := ARRAY(
                WITH candidate AS MATERIALIZED (
                    SELECT c.projection_id, c.embedding_m3 FROM crv_retrieval.chunk AS c
                    WHERE c.release_id = p_release
                    ORDER BY c.embedding_m3 <=> m3
                    LIMIT 2 * depth)
                SELECT candidate.projection_id FROM candidate
                WHERE candidate.embedding_m3 IS NOT NULL
                ORDER BY candidate.embedding_m3 <=> m3, candidate.projection_id
                LIMIT depth);
        END IF;
    ELSIF p_vector IS NOT NULL THEN
        RAISE EXCEPTION 'Unknown embedding model %', p_model;
    END IF;

    RETURN QUERY
    WITH leg AS (
        SELECT hit.id, hit.ord FROM unnest(lexical) WITH ORDINALITY AS hit(id, ord)
        UNION ALL
        SELECT hit.id, hit.ord FROM unnest(identified) WITH ORDINALITY AS hit(id, ord)
        UNION ALL
        SELECT hit.id, hit.ord FROM unnest(nearest) WITH ORDINALITY AS hit(id, ord)
    ), fused AS (
        SELECT leg.id, sum(1.0 / (60 + leg.ord))::double precision AS score
        FROM leg GROUP BY leg.id
    )
    SELECT c.projection_id, c.release_id, c.chunk_id, c.content_hash, c.title, c.content,
        c.source_uri, c.source_path, c.document_code, c.original_page_numbers,
        c.source_locators, c.original_source_uris, c.is_generated, c.document_class,
        c.citation_basis, c.extraction_route, c.original_content_hashes, fused.score
    FROM fused JOIN crv_retrieval.chunk AS c ON c.projection_id = fused.id
    ORDER BY fused.score DESC, c.projection_id
    LIMIT p_top;
END
$$;

REVOKE ALL ON FUNCTION crv_retrieval.refresh_keyword_stats(),
    crv_retrieval.keyword_leg(TEXT, TEXT, TEXT[], INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION crv_retrieval.keyword_leg(TEXT, TEXT, TEXT[], INTEGER)
    TO tizert_ingest_worker;
