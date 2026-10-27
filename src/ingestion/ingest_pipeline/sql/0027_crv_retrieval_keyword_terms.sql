-- 0027_crv_retrieval_keyword_terms: keep the keyword-leg text where a new question finds it.
-- search_text is TOASTed among the 12 KB te3 vectors of crv_retrieval.chunk (2 GB of TOAST
-- for 230 MB of tsvectors), so a question never asked before read 1,200 to 6,600 TOAST pages
-- (1 to 10 s on the 120 IOPS disk) before the 4 GiB server could cache them next to the
-- 1 GB HNSW index. chunk_terms holds only what the keyword leg reads, in line (STORAGE MAIN),
-- small enough to stay in shared buffers; a trigger keeps it equal to chunk.

-- The copy below reads every TOASTed tsvector once.
SET LOCAL statement_timeout = 0;

CREATE TABLE crv_retrieval.chunk_terms (
    projection_id TEXT PRIMARY KEY
        REFERENCES crv_retrieval.chunk (projection_id) ON DELETE CASCADE,
    release_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    search_text TSVECTOR NOT NULL
) WITH (toast_tuple_target = 8160);

ALTER TABLE crv_retrieval.chunk_terms ALTER COLUMN search_text SET STORAGE MAIN;

INSERT INTO crv_retrieval.chunk_terms (projection_id, release_id, content_hash, search_text)
SELECT c.projection_id, c.release_id, c.content_hash, c.search_text
FROM crv_retrieval.chunk AS c;

CREATE INDEX chunk_terms_search_text_idx ON crv_retrieval.chunk_terms USING gin (search_text);
CREATE INDEX chunk_terms_release_content_idx
    ON crv_retrieval.chunk_terms (release_id, content_hash);
ANALYZE crv_retrieval.chunk_terms;

-- Runs as the owner: writers of chunk need no privilege on chunk_terms.
CREATE FUNCTION crv_retrieval.copy_chunk_terms() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    INSERT INTO crv_retrieval.chunk_terms (projection_id, release_id, content_hash, search_text)
    VALUES (NEW.projection_id, NEW.release_id, NEW.content_hash, NEW.search_text)
    ON CONFLICT (projection_id) DO UPDATE
    SET release_id = EXCLUDED.release_id, content_hash = EXCLUDED.content_hash,
        search_text = EXCLUDED.search_text;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION crv_retrieval.copy_chunk_terms() FROM PUBLIC;

CREATE TRIGGER chunk_terms_copy
    AFTER INSERT OR UPDATE OF release_id, content_hash, search_text ON crv_retrieval.chunk
    FOR EACH ROW EXECUTE FUNCTION crv_retrieval.copy_chunk_terms();

-- The verification, run as the worker, explains the first stage of the keyword leg.
GRANT SELECT ON crv_retrieval.chunk_terms TO tizert_ingest_worker;

-- The keyword leg of 0026, reading chunk_terms.
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
            SELECT t.projection_id FROM crv_retrieval.chunk_terms AS t
            WHERE t.release_id = p_release AND t.search_text @@ matching
                AND (NOT scoped OR t.content_hash = ANY (hashes))
            ORDER BY ts_rank(t.search_text, terms, 1) DESC, t.projection_id
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
            SELECT t.projection_id, t.search_text
            FROM crv_retrieval.chunk_terms AS t
            WHERE t.release_id = p_release AND t.search_text @@ matching
                AND (NOT scoped OR t.content_hash = ANY (hashes))
            ORDER BY ts_rank('{1, 1, 0.5, 1}', t.search_text, terms, 1) DESC, t.projection_id
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
