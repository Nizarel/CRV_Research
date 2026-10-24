---
title: pgvector retrieval test plan
description: Plan to test Azure Database for PostgreSQL with pgvector side by side with Azure AI Search for CRV retrieval, as the first step toward running retrieval on a DGX Spark
author: CRV Research
ms.date: 2026-10-23
ms.topic: overview
keywords:
  - pgvector
  - postgresql
  - azure ai search
  - retrieval evaluation
  - dgx spark
estimated_reading_time: 10
---

## Summary

Status: **proposal**, not started, October 2026.

We want to move retrieval off Azure AI Search and eventually run it on a DGX Spark. Before that move,
we test PostgreSQL with pgvector on Azure, side by side with Azure AI Search, on the same release and
the same golden questions. Azure AI Search stays the default throughout, so nothing changes for users.

* The test store is a new `crv_retrieval` schema in the existing `ingestion-catalog` database on
  `psql-crv-awmc6icu2kueq-ncus`, scaled to General Purpose `Standard_D4ds_v5` for the test.
* Release `crv-v1` (about 145,855 chunks) is copied once from data already in PostgreSQL and blob
  storage. Nothing is extracted again, and the OpenAI vectors are reused.
* The backend gains a `PostgresSearch` implementation of the existing `Search` port, selected by
  `CRV_SEARCH_BACKEND`, which defaults to `azure`.
* BGE-M3 and bge-reranker-v2-m3 run on CPU in AKS with Hugging Face text-embeddings-inference (TEI).
  Cohere Rerank runs in Foundry.
* The existing `retrieval-eval` and `answer-eval` AKS jobs compare the configurations on the dev split.

## Decisions

* Run on the existing PostgreSQL server in a new schema, scaled up for the test, rather than on a
  dedicated server.
* Compare keyword, hybrid, LLM-reranked, cross-encoder-reranked and BGE-M3 configurations against
  Azure AI Search.
* Use two cross-encoders: bge-reranker-v2-m3, the model planned for the DGX Spark, and Cohere Rerank
  as a managed reference.
* Evaluate retrieval first, then end-to-end answers through the backend switch.
* Copy release `crv-v1` once; ingestion does not write to the PostgreSQL store on an ongoing basis.
* Host the open models on CPU in AKS. Their quality is representative; their latency is not.

## Starting point

* The backend reads evidence through the `Search` port in
  [ports/retrieval.py](../src/backend/crv_agent/ports/retrieval.py), and `AzureSearch` is its only
  implementation.
* The ingestion catalog holds chunk metadata, provenance, the knowledge graph, and an exact-lookup
  cache of `text-embedding-3-large` vectors (3,072 dimensions) keyed by chunk text hash.
* Full chunk text lives in blob artifacts (chunk-set JSONL) referenced from PostgreSQL, not in the
  database.
* The Azure AI Search index stores vectors as non-retrievable, so the vectors come from the
  PostgreSQL cache, not from Search.
* `ingest.projection_item` is keyed by `(store, target, projection_id)`, so a PostgreSQL projection
  can sit next to the Azure one.
* The [server template](../infra/core/database/postgresql-flexible-server.bicep) already allows
  `vector`, `pg_trgm` and `unaccent`.
* The CRV server runs Burstable `Standard_B1ms` (2 GiB RAM, 32 GB storage), which is too small for
  this test.
* PostgreSQL is in North Central US, while AKS, Azure AI Search and Azure OpenAI are in South Central
  US. Each database call pays a cross-region round trip of about 36 ms.

> [!NOTE]
> pgvector's standard HNSW index supports up to 2,000 dimensions. The 3,072-dimension OpenAI vectors
> are indexed through a half-precision (`halfvec`) expression index and re-scored at full precision.

## Phase 0: Preconditions

No code changes in this phase.

1. Take stock of the source data and the server:
   * Count the verified Azure rows: `ingest.projection_item` with `store=azure_search`,
     `target=crv-evidence-v1`, `release_id=crv-v1` and `state=verified`. This count is the parity
     target.
   * Check the pgvector version (iterative index scans need 0.8.0 or later), the `pg_trgm` and
     `unaccent` versions, and free storage.
   * Find the `text-embedding-3-large` deployment name on `cog-awmc6icu2kueq`.
   * Confirm that Cohere Rerank is available in the region and that sending CRV documents to it is
     approved.
   * Check the Azure AI Search semantic ranker plan. The CRV parameters set `free`, which allows
     1,000 semantic queries a month, and the evaluation runs will likely exceed that.
2. Confirm outbound access from AKS. The network security rules block the internet, so copy the
   Hugging Face model weights to blob storage or bake them into an ACR image first. The Cohere
   endpoint may need a private endpoint.
3. Scale `psql-crv-awmc6icu2kueq-ncus` to General Purpose `Standard_D4ds_v5` with the Azure CLI at a
   quiet time:
   * The server restarts, so ingestion and the agent are briefly offline. Get explicit confirmation
     before running it.
   * Leave auto-grow storage on rather than pre-growing it, because storage cannot shrink.
   * Do not redeploy [main.bicep](../infra/main.bicep) during the test. Its default
     `postgresSkuName` would scale the server back to B1ms.

## Phase 1: Schema

Depends on Phase 0.

1. Add migration `0022_crv_retrieval.sql` under
   [src/ingestion/ingest_pipeline/sql](../src/ingestion/ingest_pipeline/sql), following the pattern
   of [0017_crv_kg_read.sql](../src/ingestion/ingest_pipeline/sql/0017_crv_kg_read.sql):
   * Enable `vector`, `pg_trgm` and `unaccent`.
   * Create French and English text-search configurations that strip accents.
   * Create one table, `crv_retrieval.chunk`. It holds every `SearchRow` field, `identifiers[]`,
     `language`, `payload_sha256`, a weighted text-search column (title and identifiers above
     content), and two nullable vector columns: `embedding_te3 vector(3072)` and
     `embedding_m3 vector(1024)`. One table lets release and hash filters apply in the same scan as
     the vector search.
   * Add a B-tree index on `(release_id, content_hash)` and GIN indexes on the text-search column
     and on `identifiers`.
   * Add read functions `release_status`, `search_lexical` and `search_hybrid`. They run with their
     owner's rights (`SECURITY DEFINER`), with a fixed `search_path` and their own HNSW settings
     (`ef_search` and iterative scan).
   * Grant `EXECUTE` on the functions to `crv_kg_reader` only, and write access on the table to
     `tizert_ingest_worker`.
2. Make `search_hybrid` combine the keyword, vector and identifier results in one round trip, using
   reciprocal rank fusion with k = 60:
   * The vector step searches the half-precision index for the top 100 candidates, then re-scores
     them at full precision.
   * When a list of document hashes is given, it scans those documents exactly instead of using the
     index.
   * Keyword terms are joined with OR. Azure's simple query matches any term, while
     `websearch_to_tsquery` requires all of them.
3. Build the vector indexes after loading, not in a migration. A new CLI command,
   `retrieval-index --model te3|m3`, runs under the admin identity used for migrations. It creates a
   cosine index on `embedding_te3::halfvec(3072)` and one on `embedding_m3`, with
   `maintenance_work_mem` raised for the build.

## Phase 2: Data copy

Depends on Phase 1, step 1.

1. Move the row-building logic out of `ProjectSearchStage.execute` in
   [project_search.py](../src/ingestion/ingest_pipeline/stages/project_search.py) into a shared
   function: evidence, then units, chunks, cached vectors and `build_row`. The stage's behaviour
   stays the same.
2. Add `PostgresRetrievalProjector` in `adapters/postgres/retrieval.py`, implementing the existing
   `SearchProjector` port in [providers.py](../src/ingestion/ingest_pipeline/ports/providers.py). It
   bulk-copies rows into a staging table with `real[]` vectors, then inserts them with a `::vector`
   cast, which avoids registering a vector codec in `asyncpg`. It verifies each row by
   `payload_sha256`.
3. Add a CLI command, `retrieval-backfill --release crv-v1`, in
   [cli.py](../src/ingestion/ingest_pipeline/cli.py):
   * It walks the evidence that Azure AI Search has verified and records each row in
     `ingest.projection_item` with `store=postgres` and `target=crv_retrieval`.
   * It resumes after interruption and supports `--limit` and `--dry-run`.
   * It prints a parity report: IDs, counts and `payload_sha256` per row compared with the Azure
     rows.
4. Add a CLI command, `retrieval-embed --model bge-m3`, with a TEI adapter for the pipeline's
   existing `EmbeddingProvider` port. It reads text from `crv_retrieval.chunk`, skips chunks that
   already have a vector, validates results with `reconcile_embeddings`, and counts and reports any
   chunk the model truncates.
5. Add Kubernetes jobs for `retrieval-backfill`, `retrieval-embed` and `retrieval-index`. They use a
   digest-pinned pipeline image and the existing workload identities. They must resume cleanly,
   because the scheduled environment stop shuts AKS and PostgreSQL down mid-run.

## Phase 3: Model hosting

Runs in parallel with Phases 1 and 2.

1. Add a temporary CPU node pool in AKS, scaling from 0 to 1, with a taint so only the model servers
   run on it.
2. Deploy two TEI CPU servers, `tei-bge-m3` and `tei-bge-reranker`:
   * Each server is an internal ClusterIP service with a network policy.
   * Images are pinned by digest, and weights load from the copy made in Phase 0.
   * Reranker input is capped at 1,024 tokens (title plus the start of the content), with
     truncation on.
3. Deploy Cohere Rerank in Foundry. Prefer the existing AI Services account, so the agent's Entra
   role covers it. Otherwise, use a serverless endpoint with its key in Key Vault, mounted through
   the Secrets Store CSI driver.

## Phase 4: Backend implementation

Depends on Phase 1, step 1, and runs in parallel with Phases 2 and 3.

1. Add settings to [settings.py](../src/backend/crv_agent/runtime/settings.py), validated together:
   * `search_backend` (`azure` or `postgres`) defaults to `azure`.
   * `pg_embedding_model` (`te3` or `m3`) comes with its deployment or endpoint.
   * `pg_reranker` (`none`, `bge` or `cohere`) comes with its endpoint.
2. Add `PostgresSearch` in `crv_agent/adapters/postgres/search.py`. It implements the `Search` port,
   so the rest of the agent is unaware of the backend:
   * It opens its own connection pool with `entra_pool` (pool size made configurable), instead of
     sharing the agent's read pool, which caps at 5 connections.
   * It embeds the query through the OpenAI SDK with an Entra token for `text-embedding-3-large`
     (without LangChain or `tiktoken`), or through TEI over HTTP for BGE-M3.
   * In `semantic` mode it runs the hybrid search, then the chosen cross-encoder over the top 50.
     This mirrors the Azure semantic ranker, so `Retrieval.retrieve`, `fuse` and `focus` stay
     unchanged.
   * It keeps the hash validation of `query_filter`, reuses `SearchRow`, returns no caption, and
     records embedding, database and reranking time separately.
3. In [infrastructure.py](../src/backend/crv_agent/runtime/infrastructure.py), type
   `Infrastructure.search` as the `Search` port and build `AzureSearch` or `PostgresSearch` from the
   setting.
4. Add a `LEX` arm (keyword search, top 10) to
   [evaluation/retrieval.py](../src/backend/crv_agent/evaluation/retrieval.py), and record the
   backend in each output record.
5. Add `-SearchBackend`, `-PgEmbeddingModel`, `-PgReranker`, the model endpoints and backend labels
   to [render-research-agent.ps1](../infra/kubernetes/research-agent/render-research-agent.ps1) and
   [research-agent.yaml](../infra/kubernetes/research-agent/research-agent.yaml).
6. Write tests:
   * Backend unit tests use a fake pool, embedder and reranker. They check that rows map to the same
     `Evidence` as `AzureSearch`, that invalid hashes are rejected, and that order and top-k behave
     correctly.
   * Settings tests cover the new validation rules.
   * Ingestion unit tests cover the projector and the backfill selection.
   * The integration fixture uses a PostgreSQL image that includes pgvector; the current fixture
     only expects Apache AGE.

## Phase 5: Evaluation

Depends on Phases 2 to 4.

### Configurations

Every configuration runs the `LEX`, `A0`, `A1` and `CEIL` arms. PG-TE3-LLM is the existing `LLM` arm
(`CRV_EVIDENCE_RERANK=llm`) run on the PG-TE3 backend.

| Label      | Vectors                  | Reranker              |
|------------|--------------------------|-----------------------|
| AZ         | `text-embedding-3-large` | Azure semantic ranker |
| PG-TE3     | `text-embedding-3-large` | None                  |
| PG-TE3-LLM | `text-embedding-3-large` | Existing LLM reranker |
| PG-TE3-BGE | `text-embedding-3-large` | bge-reranker-v2-m3    |
| PG-TE3-COH | `text-embedding-3-large` | Cohere Rerank         |
| PG-M3      | BGE-M3 (1024-d)          | None                  |
| PG-M3-BGE  | BGE-M3 (1024-d)          | bge-reranker-v2-m3    |

PG-M3-BGE is the configuration planned for the DGX Spark.

### Steps

1. Smoke test with the backend set to PostgreSQL: run `agent-check`, then `retrieval-eval -Only` on
   5 questions.
2. Run the retrieval matrix on the dev split in one window, scored three times by the shared judge.
3. Run the end-to-end answer evaluation on the dev split for AZ, the best PostgreSQL configuration,
   and PG-M3-BGE. The held-out split is not run.
4. Check vector search quality and query plans:
   * Approximate search should agree with exact search on at least 95% of the top 50 for the golden
     questions.
   * `EXPLAIN (ANALYZE, BUFFERS)` should show the HNSW index for unscoped searches and the B-tree for
     hash-scoped ones.
5. Write the results in [Docs/evaluation](./evaluation), in the format of
   [golden-retrieval-baseline-2026-10-21.md](./evaluation/golden-retrieval-baseline-2026-10-21.md).

## Phase 6: Cleanup

1. Scale PostgreSQL back down unless we adopt it.
2. Remove the CPU node pool and the TEI servers, and delete the Cohere deployment.
3. Leave the backend on `azure`. The `crv_retrieval` schema is unused and harmless.

## Verification

1. Run the backend checks in `src/backend`, then the matching checks in `src/ingestion`:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
   python -m mypy
   python -m ruff check crv_agent tests
   .\.venv\Scripts\lint-imports.exe
   ```

2. Run the SQL integration tests in Linux Docker, because Windows ARM64 development machines have no
   `asyncpg` wheel.
3. Check copy parity. The PostgreSQL row count must equal the verified Azure count, the
   `projection_id` sets must match, and `payload_sha256` must match per row. Investigate mismatches
   rather than ignoring them.
4. Check the stored vectors. Re-embed 50 random chunks; cosine similarity with the stored
   `text-embedding-3-large` vectors should be about 1.0.
5. Check vector search quality and query plans as in Phase 5, step 4.
6. Run the `agent-check` smoke test and the 5-question retrieval smoke test with the backend set to
   PostgreSQL.

## Adoption gates

Earlier runs showed judge noise of about 2 to 3 points between identical runs, so the gates allow a
small margin. See [agent-answers-2026-10-21.md](./evaluation/agent-answers-2026-10-21.md) for the
answer-evaluation baseline.

| Metric                 | Gate              |
|------------------------|-------------------|
| `A1` judge@10          | Within 0.03 of AZ |
| `CEIL` anchor@50       | Within 0.02 of AZ |
| Citation contract      | Unchanged from AZ |
| ANN vs exact recall@50 | At least 0.95     |

Latency is reported as embedding, database and reranking time, with the cross-region round trip
shown separately. Latency of the CPU-hosted models is reported but not gated.

## Risks and open questions

* The `free` semantic ranker plan will likely throttle the Azure runs. We recommend the Standard plan
  for the test month, or running AZ first and reusing its results.
* Cohere Rerank sends CRV documents to a Foundry partner model. If that is not acceptable, we drop
  PG-TE3-COH; bge-reranker-v2-m3 still covers the cross-encoder comparison.
* Re-embedding about 146,000 chunks with BGE-M3 on CPU is a long-running job that must resume across
  scheduled environment stops.
* A [main.bicep](../infra/main.bicep) deployment during the test would scale PostgreSQL back to B1ms.
* If we adopt PostgreSQL, a later phase adds ongoing dual-write during ingestion and pins the General
  Purpose SKU in
  [main.crv.southcentralus.parameters.json](../infra/main.crv.southcentralus.parameters.json).

## Out of scope

* Running retrieval on the DGX Spark itself
* Local LLMs, local OCR and local extraction
* Moving PostgreSQL to South Central US
* PostgreSQL modes in the ingestion golden tool (`tools/golden`), because the backend evaluation
  already does the shared golden scoring
* Changes to the deployed web app

## Relevant files

* [project_search.py](../src/ingestion/ingest_pipeline/stages/project_search.py) holds `build_row`,
  `payload_digest` and `ProjectSearchStage.execute`.
* [providers.py](../src/ingestion/ingest_pipeline/ports/providers.py) defines the `SearchProjector`
  and `EmbeddingProvider` ports.
* [adapters/postgres/evidence.py](../src/ingestion/ingest_pipeline/adapters/postgres/evidence.py)
  reads the cached vectors with `get_many`.
* [vectors.py](../src/ingestion/ingest_pipeline/domain/vectors.py) provides `reconcile_embeddings`
  and `encode_f32le`.
* [0017_crv_kg_read.sql](../src/ingestion/ingest_pipeline/sql/0017_crv_kg_read.sql) shows the
  `SECURITY DEFINER` and `crv_kg_reader` grant pattern.
* [cli.py](../src/ingestion/ingest_pipeline/cli.py) hosts the new commands, modelled on
  `_kg_grant_reader` and `_migrate`.
* [adapters/search.py](../src/backend/crv_agent/adapters/search.py) holds `SearchRow`,
  `query_filter` and the evidence mapping to match.
* [ports/retrieval.py](../src/backend/crv_agent/ports/retrieval.py) defines the `Search` port.
* [adapters/postgres/memory.py](../src/backend/crv_agent/adapters/postgres/memory.py) provides
  `entra_pool` and its read-only `crv_kg_reader` connection.
* [services/retrieval.py](../src/backend/crv_agent/services/retrieval.py) holds `retrieve`, `fuse`
  and `focus`, which stay unchanged.
* [infrastructure.py](../src/backend/crv_agent/runtime/infrastructure.py) and
  [settings.py](../src/backend/crv_agent/runtime/settings.py) choose the backend.
* [evaluation/retrieval.py](../src/backend/crv_agent/evaluation/retrieval.py) defines the `ARMS`
  list and gets the new `LEX` arm.
* [render-research-agent.ps1](../infra/kubernetes/research-agent/render-research-agent.ps1) and
  [research-agent.yaml](../infra/kubernetes/research-agent/research-agent.yaml) render the
  evaluation jobs.

## References

* pgvector: <https://github.com/pgvector/pgvector>
* BGE-M3: <https://huggingface.co/BAAI/bge-m3>
* bge-reranker-v2-m3: <https://huggingface.co/BAAI/bge-reranker-v2-m3>
