-- 0023_crv_retrieval_plans: keep crv_retrieval.search on its HNSW and GIN indexes. Vectors and
-- tsvectors are stored out of line (TOAST), which the planner does not cost, so a full scan and
-- sort of every row looked cheaper than the indexes: about 3 s per search on 145,855 rows with
-- the PostgreSQL defaults, and on Azure whenever a cost setting tipped the balance. Results keep
-- coming from the same legs; only the access paths are pinned.
ALTER FUNCTION crv_retrieval.search(TEXT, TEXT, TEXT[], TEXT, REAL[], INTEGER)
    SET enable_seqscan = off;
ALTER FUNCTION crv_retrieval.search(TEXT, TEXT, TEXT[], TEXT, REAL[], INTEGER)
    SET enable_sort = off;
