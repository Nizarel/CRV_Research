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

Status: **Stage 1 code implemented** (2026-10-23), not yet run on Azure. Stage 2 is a plan.

We want to move retrieval off Azure AI Search and run it on two DGX Spark machines. The move is
staged so that each quality change can be traced to one cause:

1. Stage 1 tests PostgreSQL with pgvector on Azure, side by side with Azure AI Search, on the same
   release, the same `text-embedding-3-large` (TE3) vectors and the same golden questions. Only the
   search engine changes.
2. Stage 2 restores that database on DGX Spark 1, then adds BGE-M3 and bge-reranker-v2-m3 on the GPU.

Azure AI Search stays the default throughout, and the deployed web app keeps using it.

* The store is a new `crv_retrieval` schema in the existing `ingestion-catalog` database on
  `psql-crv-awmc6icu2kueq-ncus`, scaled to General Purpose `Standard_D4ds_v5` for the test. No new
  database is created.
* Release `crv-v1` (about 145,855 chunks) is copied once from the evidence and embedding cache
  already in PostgreSQL and blob storage. Document Intelligence and Content Understanding are not
  rerun, and no new embeddings are paid for.
* The backend gains `PostgresSearch`, a second implementation of the existing `Search` port,
  selected per evaluation job with `CRV_SEARCH_BACKEND=postgres`.
* The existing `retrieval-eval`, `agent-check` and `answer-eval` AKS jobs compare the two backends.

## Decisions

* Use the existing PostgreSQL server and a new schema, scaled up for the test.
* In Stage 1, compare Azure AI Search with PostgreSQL TE3 alone, with the existing LLM reranker, and
  with Cohere Rerank. BGE-M3 and bge-reranker-v2-m3 move to the DGX GPU in Stage 2.
* Evaluate retrieval first, then end-to-end answers through the backend switch.
* Copy release `crv-v1` once; ingestion does not write to `crv_retrieval` on an ongoing basis.
* Stage 2 ends with local retrieval only: Azure OpenAI LLMs and Azure ingestion stay. The Sparks run
  Docker Compose on DGX OS.
* No time estimates: a 50-document copy and the index build give measured throughput.

## Starting point

* The backend reads evidence through the `Search` port in
  [ports/retrieval.py](../src/backend/crv_agent/ports/retrieval.py): `query`, `tables` (neighbouring
  table chunks) and `ready`.
* The ingestion catalog holds chunk metadata, provenance, the knowledge graph, and an exact-lookup
  cache of TE3 vectors (3,072 dimensions) keyed by chunk text hash.
* Full chunk text lives in blob artifacts (chunk-set JSONL) referenced from PostgreSQL, not in the
  database.
* The Azure AI Search index stores vectors as non-retrievable, so the vectors come from the
  PostgreSQL cache, not from Search.
* `ingest.projection_item` is keyed by `(store, target, projection_id)`, so a PostgreSQL projection
  sits next to the Azure one.
* The [server template](../infra/core/database/postgresql-flexible-server.bicep) already allows
  `vector`, `pg_trgm` and `unaccent`.
* The CRV server runs Burstable `Standard_B1ms` (2 GiB RAM, 32 GB storage), which is too small for
  this test.
* PostgreSQL is in North Central US, while AKS, Azure AI Search and Azure OpenAI are in South Central
  US. Each database call pays a cross-region round trip of about 36 ms.

> [!NOTE]
> pgvector's standard HNSW index supports up to 2,000 dimensions. The 3,072-dimension TE3 vectors
> are indexed through a half-precision (`halfvec`) expression index and re-ranked at full precision.

## Implemented

### Schema

[0022_crv_retrieval.sql](../src/ingestion/ingest_pipeline/sql/0022_crv_retrieval.sql) needs
pgvector 0.8.0 or later and creates:

* French, English and `simple` text-search configurations, all accent-insensitive. The `simple`
  one drops the stop words of both languages.
* `crv_retrieval.chunk`: every `SearchRow` field, `chunk_kind`, folded `identifier_keys`, a weighted
  `search_text` (title and identifiers, then paths, then content) and two vector columns,
  `embedding_te3 vector(3072)` and `embedding_m3 vector(1024)`.
* `crv_retrieval.search`: reciprocal rank fusion (k = 60) of a keyword leg (any query word, as the
  Search simple syntax), an exact identifier leg (the index's identifier analyzers) and a vector
  leg, each 50 deep. The vector leg reads 100 HNSW candidates and re-ranks them at full precision;
  a content-hash scope is searched exactly.
* `crv_retrieval.table_neighbors` and `crv_retrieval.release_status`.
* `EXECUTE` for `crv_kg_reader` only (no table access), and table writes for
  `tizert_ingest_worker`.

### Copy and index

* `projection_rows` in
  [project_search.py](../src/ingestion/ingest_pipeline/stages/project_search.py) is the projection
  stage's row building, now shared; the stage is unchanged.
* `PostgresRetrievalProjector` in
  [retrieval.py](../src/ingestion/ingest_pipeline/adapters/postgres/retrieval.py) implements the
  `SearchProjector` port: one statement per document, verified by `payload_sha256`.
* `crv-pipeline retrieval-backfill` in
  [retrieval_copy.py](../src/ingestion/ingest_pipeline/runtime/retrieval_copy.py) rebuilds the rows
  of every content with verified Search rows, compares each digest with the Search row, copies, and
  records the copy in `ingest.projection_item` (`store=postgres`). It resumes after interruption and
  takes `--limit`, `--dry-run`, `--allow-drift` and `--refresh`. Drifted contents are skipped and
  reported unless allowed.
* `crv-pipeline retrieval-index --model te3|m3` builds the HNSW index as the table owner.
* Jobs `retrieval-backfill` and `retrieval-index` in
  [render-pilot.ps1](../infra/kubernetes/pipeline/render-pilot.ps1).

### Backend

* `PostgresSearch` in [search.py](../src/backend/crv_agent/adapters/postgres/search.py): the same
  `Evidence` as `AzureSearch`, the same hash and table-scope validation, no captions. In `semantic`
  mode an optional cross-encoder reorders the top 50.
* [retrieval_models.py](../src/backend/crv_agent/adapters/retrieval_models.py): TE3 query vectors
  from the corpus deployment (one call per distinct text, shared by concurrent legs) and a Cohere
  Rerank client, both with Entra tokens and bounded retries.
* Settings `search_backend`, `pg_embedding_deployment`, `pg_search_pool_size`, `pg_reranker`,
  `pg_reranker_url` and `pg_reranker_model`.
* A `LEX` arm (keyword search, top 10) and `--label` (arm prefix such as `PG_A1`) in the retrieval
  evaluation.
* [render-research-agent.ps1](../infra/kubernetes/research-agent/render-research-agent.ps1):
  `-SearchBackend`, `-PgReranker`, `-PgRerankerUrl`, `-PgRerankerModel` and `-ArmLabel`, set on the
  job containers only. The web app shares the ConfigMap and stays on Azure.

### Tested

* Unit tests: projector, copy (identical, drifted, dry run, failures), index parameters, adapter
  mapping and ordering, cross-encoder, query embedder, settings and the `LEX` arm.
* SQL against PostgreSQL 16 + Apache AGE 1.6.0 + pgvector 0.8.7
  ([postgres.Dockerfile](../src/ingestion/tests/integration/postgres.Dockerfile)): all migrations
  apply; stemming, accent folding and stop words; identifiers; vector and scoped legs; table
  neighbours; reader access denied on the table; input validation. With 20,000 vectors the planner
  uses the half-precision HNSW index under the release filter.

## Stage 1: Azure PostgreSQL side by side

### Preconditions

No code changes in this step.

1. Take stock of the source data and the server:
   * Count the verified Azure rows: `ingest.projection_item` with `store=azure_search`,
     `target=crv-evidence-v1`, `release_id=crv-v1` and `state=verified`. This count is the parity
     target.
   * Run `crv-pipeline status` for failed or partly ingested documents.
   * Check that the server offers pgvector 0.8.0 or later, and check free storage.
   * Confirm that Cohere Rerank is available and that sending CRV documents to it is approved.
   * Check the Azure AI Search semantic ranker plan. The CRV parameters set `free`, which allows
     1,000 semantic queries a month, and the evaluation runs will likely exceed that.
2. Scale `psql-crv-awmc6icu2kueq-ncus` to General Purpose `Standard_D4ds_v5` with
   `az postgres flexible-server update --tier GeneralPurpose --sku-name Standard_D4ds_v5`:
   * The server restarts, so ingestion and the agent are briefly offline. Get explicit confirmation
     before running it.
   * Leave auto-grow storage on rather than pre-growing it, because storage cannot shrink.
   * Do not redeploy [main.bicep](../infra/main.bicep) during the test. Its default
     `postgresSkuName` would scale the server back to B1ms.

### Run order

Build and push the pipeline and agent images first, and pass their digests to the render scripts.

1. Apply migration 0022 as the Entra admin: `render-pilot.ps1 -Job dbsetup` (dry run), then again
   with `-DbApply`.
2. Compare digests without writing: `render-pilot.ps1 -Job retrieval-backfill -RetrievalDryRun
   -RetrievalLimit 50`. The report lists identical rows, drift and problems.
3. Copy 50 documents for real (`-RetrievalLimit 50`) and read `rows_per_second` from the report.
4. Copy the rest (no limit). Rerun the job until the report shows `contents: 0`; an interrupted run
   resumes where it stopped.
5. Build the vector index: `render-pilot.ps1 -Job retrieval-index`, and note the reported time.
6. Smoke test: `render-research-agent.ps1 -Job agent-check -SearchBackend postgres`, then
   `-Job retrieval-eval -SearchBackend postgres -Only <5 ids>`.

### Configurations

Every configuration runs the `LEX`, `A0`, `A1` and `CEIL` arms. `-ArmLabel` names the arms of each
run. The LLM reranker is the existing `LLM` arm.

| Label | Search                | Reranker              | Render arguments                          |
|-------|-----------------------|-----------------------|-------------------------------------------|
| AZ    | Azure AI Search       | Azure semantic ranker | `-ArmLabel AZ`                            |
| PG    | PostgreSQL TE3        | None                  | `-SearchBackend postgres -ArmLabel PG`    |
| PGLLM | PostgreSQL TE3        | Existing LLM reranker | As PG, with `-Arm LLM`                    |
| PGCOH | PostgreSQL TE3        | Cohere Rerank         | As PG, with `-PgReranker cohere` and URL  |

### Steps

1. Run the retrieval matrix on the dev split in one window, scored three times by the shared judge.
2. Check vector search quality and query plans:
   * Approximate search should agree with exact search on at least 95% of the top 50 for the golden
     questions.
   * `EXPLAIN (ANALYZE, BUFFERS)` should show the HNSW index for unscoped searches and the B-tree for
     hash-scoped ones.
3. If PostgreSQL `LEX` clearly trails Azure `LEX`, add BM25 scoring (a term-statistics table) and
   rerun: PostgreSQL's `ts_rank` has no inverse document frequency.
4. Run the end-to-end answer evaluation on the dev split for AZ and the best PostgreSQL
   configuration (`render-research-agent.ps1 -Job answer-eval -SearchBackend postgres`). The
   held-out split is not run.
5. Write the results in [Docs/evaluation](./evaluation), in the format of
   [golden-retrieval-baseline-2026-10-21.md](./evaluation/golden-retrieval-baseline-2026-10-21.md).
6. Stage 2 starts only if the adoption gates pass.

## Stage 2: DGX Spark (search only)

Not started.

1. Platform: Spark 1 runs PostgreSQL 16 with AGE 1.6 and pgvector (the
   [test image](../src/ingestion/tests/integration/postgres.Dockerfile) is the starting point), the
   GPU model server, the backend and the web front end. Spark 2 runs a PostgreSQL standby, the
   backups and the evaluation runner. A site-to-site VPN with private DNS forwarding reaches Azure
   AI Search, Azure OpenAI and Blob Storage; a certificate service principal replaces workload
   identity.
2. Portability: ARM64 images (`docker buildx --platform linux/amd64,linux/arm64`), a password or
   client-certificate PostgreSQL login in the backend beside `entra_pool`, and Docker Compose with
   systemd units.
3. Models: choose TEI or vLLM on the GB10 GPU for BGE-M3 and bge-reranker-v2-m3, then add a TEI
   query embedder and cross-encoder beside the Cohere one, and a `retrieval-embed` command that
   fills `embedding_m3`. `retrieval-index --model m3` already exists.
4. Data: dump `ingestion-catalog` from Azure PostgreSQL and restore it on Spark 1, with local roles
   created first. Check `release_status` and the digests against Azure, then sync each new release
   over the VPN while ingestion stays on Azure.
5. Evaluate in one window against live Azure AI Search: PostgreSQL TE3 on DGX (must match Stage 1
   within noise), TE3 with bge-reranker, BGE-M3, and BGE-M3 with bge-reranker.
6. Run in parallel with AKS, then switch search to DGX. Retiring Azure AI Search is a separate
   decision.

## Cleanup

1. Scale PostgreSQL back down unless we adopt it, and delete the Cohere deployment.
2. Leave the backend on `azure`. The `crv_retrieval` schema is unused and harmless.

## Verification

1. Run the backend checks in `src/backend`, then the matching checks in `src/ingestion`:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
   python -m mypy
   python -m ruff check crv_agent tests
   .\.venv\Scripts\lint-imports.exe
   ```

2. Run the SQL integration tests in Linux Docker, because Windows ARM64 development machines have no
   `asyncpg` wheel. The `pg_dsn` fixture builds
   [postgres.Dockerfile](../src/ingestion/tests/integration/postgres.Dockerfile); with
   `TIZ_TEST_PG_DSN` it uses an existing database instead.
3. Check copy parity in the `retrieval-backfill` report. Every content should be identical; drifted
   contents (projection code or placements changed since the Search upload) are skipped unless
   `--allow-drift` is set. Investigate drift rather than ignoring it.
4. Check the stored vectors. Re-embed 50 random chunks; cosine similarity with the stored TE3
   vectors should be about 1.0.
5. Check vector search quality and query plans as in Stage 1, step 2.
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
shown separately.

## Risks and open questions

* The `free` semantic ranker plan will likely throttle the Azure runs. We recommend the Standard plan
  for the test month, or running AZ first and reusing its results.
* Cohere Rerank sends CRV documents to a Foundry partner model. If that is not acceptable, we drop
  PGCOH; bge-reranker-v2-m3 covers the cross-encoder comparison in Stage 2. The Cohere client
  assumes the v2 rerank request shape; check it against the Foundry deployment before the run.
* PostgreSQL keyword ranking (`ts_rank`) is not BM25. The `LEX` arm measures the gap, and step 3 of
  Stage 1 closes it if needed.
* Retrieval-eval scores each run inside its pod, so AZ and PG comparisons are unpaired unless the
  contexts are exported and scored together.
* A [main.bicep](../infra/main.bicep) deployment during the test would scale PostgreSQL back to B1ms.
* If we adopt PostgreSQL, a later phase adds ongoing dual-write during ingestion and pins the General
  Purpose SKU in
  [main.crv.southcentralus.parameters.json](../infra/main.crv.southcentralus.parameters.json).

## Out of scope

* Local LLMs, local OCR and local extraction (DI and CU are not rerun; ingestion stays on Azure)
* Moving PostgreSQL to South Central US
* PostgreSQL modes in the ingestion golden tool (`tools/golden`), because the backend evaluation
  already does the shared golden scoring
* Changes to the deployed web app, and retiring Azure AI Search

## Relevant files

* [0022_crv_retrieval.sql](../src/ingestion/ingest_pipeline/sql/0022_crv_retrieval.sql) holds the
  schema, text analysis and search functions.
* [project_search.py](../src/ingestion/ingest_pipeline/stages/project_search.py) holds `build_row`,
  `payload_digest` and the shared `projection_rows`.
* [adapters/postgres/retrieval.py](../src/ingestion/ingest_pipeline/adapters/postgres/retrieval.py)
  holds the projector, the copy work list and the index build.
* [runtime/retrieval_copy.py](../src/ingestion/ingest_pipeline/runtime/retrieval_copy.py) holds the
  resumable release copy and its parity report.
* [cli.py](../src/ingestion/ingest_pipeline/cli.py) hosts `retrieval-backfill` and
  `retrieval-index`.
* [crv-pilot-jobs.yaml](../infra/kubernetes/pipeline/crv-pilot-jobs.yaml) and
  [render-pilot.ps1](../infra/kubernetes/pipeline/render-pilot.ps1) render the copy and index jobs.
* [adapters/postgres/search.py](../src/backend/crv_agent/adapters/postgres/search.py) implements
  `PostgresSearch`.
* [adapters/retrieval_models.py](../src/backend/crv_agent/adapters/retrieval_models.py) holds the
  query embedder and the Cohere reranker.
* [adapters/search.py](../src/backend/crv_agent/adapters/search.py) holds `SearchRow` and the shared
  hash and table-scope validation.
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
