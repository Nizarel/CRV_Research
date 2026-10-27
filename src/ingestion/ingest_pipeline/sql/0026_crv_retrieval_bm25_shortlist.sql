-- 0026_crv_retrieval_bm25_shortlist: make the BM25 keyword leg (0025) as fast as ts_rank was.
-- 0025 scored every matching chunk in SQL (6,000 to 13,000 of them per question) and recomputed
-- the inverse document frequencies for each one: 1 to 2.5 s per question with every page cached,
-- against 0.2 s for the ts_rank leg of 0024. The leg now keeps the best `shortlist` chunks by a
-- C-level ts_rank that counts words as BM25 does (paths half, other fields whole), then ranks
-- only those by exact BM25 with the frequencies computed once. On 100 business questions the
-- shortlist of 600 kept 0.996 of the exact BM25 top 10 and 0.974 of its top 50.

CREATE OR REPLACE FUNCTION crv_retrieval.keyword_leg(
    p_release TEXT, p_text TEXT, p_hashes TEXT[], p_depth INTEGER)
    RETURNS TEXT[]
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    SET enable_seqscan = off
    SET enable_sort = off
AS $$
#variable_conflict use_column
DECLARE
    shortlist CONSTANT INTEGER := 600;
    hashes TEXT[] := coalesce(p_hashes, '{}');
    scoped BOOLEAN := cardinality(coalesce(p_hashes, '{}')) > 0;
    terms tsquery := crv_retrieval.query_terms(coalesce(p_text, ''));
    matching tsquery;
    stats crv_retrieval.release_stats;
    words TEXT[];
    idfs DOUBLE PRECISION[];
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
    SELECT array_agg(w.lexeme), array_agg(ln(1 + (stats.chunks - coalesce(l.chunks, 1) + 0.5)
            / (coalesce(l.chunks, 1) + 0.5))::double precision)
        INTO words, idfs
    FROM unnest(crv_retrieval.query_lexemes(coalesce(p_text, ''))) AS w(lexeme)
    LEFT JOIN crv_retrieval.lexeme_stats AS l
        ON l.release_id = p_release AND l.lexeme = w.lexeme;
    RETURN ARRAY(
        WITH short AS (
            -- ts_rank weights are {D, C, B, A}.
            SELECT c.projection_id, c.search_text
            FROM crv_retrieval.chunk AS c
            WHERE c.release_id = p_release AND c.search_text @@ matching
                AND (NOT scoped OR c.content_hash = ANY (hashes))
            ORDER BY ts_rank('{1, 1, 0.5, 1}', c.search_text, terms, 1) DESC, c.projection_id
            LIMIT greatest(shortlist, p_depth)
        ), candidate AS MATERIALIZED (
            SELECT short.projection_id, short.search_text, length(short.search_text) AS size
            FROM short
        ), word AS MATERIALIZED (
            SELECT w.lexeme, w.idf FROM unnest(words, idfs) AS w(lexeme, idf)
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
