-- 0022_crv_retrieval: evidence search in PostgreSQL beside Azure AI Search (side-by-side test,
-- then the DGX Spark move). One row per projected Search row of a release, built by the same
-- code, so payload digests are comparable. The read functions run as their owner: the agent's
-- crv_kg_reader login needs EXECUTE only. HNSW indexes are built by `retrieval-index` after a
-- bulk load, not here. Needs pgvector 0.8.0 or later (iterative index scans).

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE SCHEMA crv_retrieval;
REVOKE ALL ON SCHEMA crv_retrieval FROM PUBLIC;

-- Accent-insensitive French and English stemming, plus exact tokens without the stop words of
-- either language (the first stop-word dictionary passes other words on).
CREATE TEXT SEARCH DICTIONARY crv_retrieval.simple_en (
    TEMPLATE = pg_catalog.simple, STOPWORDS = english, ACCEPT = false);
CREATE TEXT SEARCH DICTIONARY crv_retrieval.simple_fr (
    TEMPLATE = pg_catalog.simple, STOPWORDS = french);

CREATE TEXT SEARCH CONFIGURATION crv_retrieval.fr (COPY = pg_catalog.french);
ALTER TEXT SEARCH CONFIGURATION crv_retrieval.fr
    ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.french_stem;

CREATE TEXT SEARCH CONFIGURATION crv_retrieval.en (COPY = pg_catalog.english);
ALTER TEXT SEARCH CONFIGURATION crv_retrieval.en
    ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.english_stem;

CREATE TEXT SEARCH CONFIGURATION crv_retrieval.simple (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION crv_retrieval.simple
    ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword, hword_part, numword,
        numhword, hword_numpart
    WITH public.unaccent, crv_retrieval.simple_en, crv_retrieval.simple_fr;

-- As the Search identifier analyzers: the whole value, lower-cased and ASCII-folded.
CREATE FUNCTION crv_retrieval.fold(p_value TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN lower(public.unaccent('public.unaccent'::regdictionary, p_value));

CREATE FUNCTION crv_retrieval.identifier_keys(p_values TEXT[]) RETURNS TEXT[]
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN ARRAY(
        SELECT DISTINCT crv_retrieval.fold(btrim(value))
        FROM unnest(p_values) AS value
        WHERE btrim(value) <> ''
        ORDER BY 1);

-- Query side of the identifier analyzer: the identifier_query_tokenizer pattern of the index.
CREATE FUNCTION crv_retrieval.query_identifiers(p_text TEXT) RETURNS TEXT[]
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN crv_retrieval.identifier_keys(
        regexp_split_to_array(p_text, '[\s,;:!?()\[\]{}<>"''«»]+|\.(?=\s|$)'));

-- File names and paths: the default parser would keep "a/b.pdf" as one file token.
CREATE FUNCTION crv_retrieval.path_words(p_value TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN regexp_replace(p_value, '[/\\_.]+', ' ', 'g');

-- Content in French and English as the fr.microsoft/en.microsoft fields (unknown language in
-- both), exact tokens for codes, and title, identifiers and paths weighted above content.
CREATE FUNCTION crv_retrieval.document_vector(
    p_title TEXT, p_identifiers TEXT[], p_paths TEXT[], p_content TEXT, p_language TEXT)
    RETURNS tsvector
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN setweight(to_tsvector('crv_retrieval.simple', concat_ws(' ',
            crv_retrieval.path_words(p_title), array_to_string(p_identifiers, ' '))), 'A')
        || setweight(to_tsvector('crv_retrieval.simple',
            crv_retrieval.path_words(coalesce(array_to_string(p_paths, ' '), ''))), 'B')
        || setweight(CASE WHEN p_language IS NULL OR p_language = 'fr'
            THEN to_tsvector('crv_retrieval.fr', coalesce(p_content, '')) ELSE ''::tsvector END,
            'C')
        || setweight(CASE WHEN p_language IS NULL OR p_language = 'en'
            THEN to_tsvector('crv_retrieval.en', coalesce(p_content, '')) ELSE ''::tsvector END,
            'C')
        || to_tsvector('crv_retrieval.simple', coalesce(p_content, ''));

-- Any query word (as the Search simple syntax with searchMode any), stop words of both
-- languages removed first. NULL when nothing searchable remains. The tsvector text form
-- quotes every lexeme, so joining the quoted lexemes with "|" is a valid tsquery.
-- The Snowball French list lacks "les" and loses its accented words to unaccent ("à" -> "a").
CREATE FUNCTION crv_retrieval.query_terms(p_text TEXT) RETURNS tsquery
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
BEGIN ATOMIC
    SELECT nullif(replace(strip(
            to_tsvector('crv_retrieval.fr', cleaned.words)
            || to_tsvector('crv_retrieval.en', cleaned.words)
            || to_tsvector('crv_retrieval.simple', cleaned.words))::text,
        ''' ''', ''' | '''), '')::tsquery
    FROM (SELECT array_to_string(ARRAY(
        SELECT word
        FROM unnest(tsvector_to_array(to_tsvector('crv_retrieval.simple', p_text))) AS word
        WHERE word <> ALL ('{a,les,est,sont,ont,cette,cet,meme,ete,etre,etant,etait,etaient}'::text[])
    ), ' ') AS words) AS cleaned;
END;

CREATE TABLE crv_retrieval.chunk (
    projection_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    chunk_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    source_path TEXT,
    document_code TEXT,
    identifier_keys TEXT[] NOT NULL,
    original_page_numbers INTEGER[] NOT NULL,
    source_locators TEXT[] NOT NULL,
    original_source_uris TEXT[] NOT NULL,
    original_content_hashes TEXT[] NOT NULL,
    is_generated BOOLEAN NOT NULL,
    document_class TEXT,
    citation_basis TEXT,
    extraction_route TEXT,
    language TEXT,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    search_text TSVECTOR NOT NULL,
    embedding_te3 VECTOR(3072),
    embedding_m3 VECTOR(1024),
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (release_id, chunk_id)
);

-- Vectors are not compressible: skip the compression attempt on every TOAST write.
ALTER TABLE crv_retrieval.chunk
    ALTER COLUMN embedding_te3 SET STORAGE EXTERNAL,
    ALTER COLUMN embedding_m3 SET STORAGE EXTERNAL;

CREATE INDEX chunk_release_content_idx ON crv_retrieval.chunk (release_id, content_hash);
CREATE INDEX chunk_search_text_idx ON crv_retrieval.chunk USING gin (search_text);
CREATE INDEX chunk_identifier_keys_idx ON crv_retrieval.chunk USING gin (identifier_keys);

CREATE FUNCTION crv_retrieval.release_status(p_release TEXT)
    RETURNS TABLE (chunks BIGINT, te3 BIGINT, m3 BIGINT)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT count(*), count(c.embedding_te3), count(c.embedding_m3)
    FROM crv_retrieval.chunk AS c
    WHERE c.release_id = p_release
$$;

-- Reciprocal-rank fusion (k = 60) of a keyword leg, an exact identifier leg and, with a query
-- vector, a nearest-neighbour leg, each 50 deep. Unscoped vector search reads the HNSW index
-- (half precision for 3,072 dimensions) for 100 candidates and re-ranks them at full precision;
-- a content-hash scope is searched exactly.
CREATE FUNCTION crv_retrieval.search(
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

    IF terms IS NOT NULL THEN
        lexical := ARRAY(
            SELECT c.projection_id FROM crv_retrieval.chunk AS c
            WHERE c.release_id = p_release AND c.search_text @@ terms
                AND (NOT scoped OR c.content_hash = ANY (hashes))
            ORDER BY ts_rank(c.search_text, terms, 1) DESC, c.projection_id
            LIMIT depth);
    END IF;

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

-- Table chunks of the given pages of each content (AzureSearch.tables): {hash: [pages]}.
CREATE FUNCTION crv_retrieval.table_neighbors(p_release TEXT, p_scopes JSONB)
    RETURNS TABLE (projection_id TEXT, release_id TEXT, chunk_id TEXT, content_hash TEXT,
        title TEXT, content TEXT, source_uri TEXT, source_path TEXT, document_code TEXT,
        original_page_numbers INTEGER[], source_locators TEXT[], original_source_uris TEXT[],
        is_generated BOOLEAN, document_class TEXT, citation_basis TEXT, extraction_route TEXT,
        original_content_hashes TEXT[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT c.projection_id, c.release_id, c.chunk_id, c.content_hash, c.title, c.content,
        c.source_uri, c.source_path, c.document_code, c.original_page_numbers,
        c.source_locators, c.original_source_uris, c.is_generated, c.document_class,
        c.citation_basis, c.extraction_route, c.original_content_hashes
    FROM jsonb_each(p_scopes) AS scope(content_hash, pages)
    JOIN crv_retrieval.chunk AS c
        ON c.release_id = p_release AND c.content_hash = scope.content_hash
        AND c.chunk_kind = 'table' AND NOT c.is_generated
        AND c.original_page_numbers && ARRAY(
            SELECT page::integer FROM jsonb_array_elements_text(scope.pages) AS page)
    ORDER BY c.content_hash, c.original_page_numbers, c.projection_id
    LIMIT 100
$$;

REVOKE ALL ON FUNCTION crv_retrieval.release_status(TEXT),
    crv_retrieval.search(TEXT, TEXT, TEXT[], TEXT, REAL[], INTEGER),
    crv_retrieval.table_neighbors(TEXT, JSONB) FROM PUBLIC;

GRANT USAGE ON SCHEMA crv_retrieval TO crv_kg_reader, tizert_ingest_worker;
GRANT EXECUTE ON FUNCTION crv_retrieval.release_status(TEXT),
    crv_retrieval.search(TEXT, TEXT, TEXT[], TEXT, REAL[], INTEGER),
    crv_retrieval.table_neighbors(TEXT, JSONB) TO crv_kg_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON crv_retrieval.chunk TO tizert_ingest_worker;
