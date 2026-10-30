---
title: CRV Edge Validation Kit
description: Pinned inputs, comparison arms, commands, thresholds, latency protocol, and the Azure-independence test for the CRV edge deployment on two DGX Spark hosts.
author: CRV Research
ms.date: 2026-10-29
ms.topic: how-to
keywords:
  - dgx spark
  - edge
  - evaluation
  - bge-m3
  - golden set
estimated_reading_time: 12
---

## Purpose

Prove, with the same questions and the same judge, that the edge retrieves and answers at least as
well as the Azure baseline, then prove it keeps working with Azure unreachable. The EDGE-M3
contexts and the m3 recall check ran on 2026-10-29 ([progress](#progress-2026-10-29)); scoring and
the other arms have not run yet. Every command is a template; replace each `REPLACE_*` value and
keep secrets out of command lines.

## Pinned inputs

| Input | Value |
|-------|-------|
| Primary golden set | `golden/crv_golden.jsonl`, SHA-256 `d5eb3a0279860526490562cd481711f07c0c9f9793172991585f0a0b763b87cd`, 76 questions |
| Primary dev split | 53 questions: A 24, B 10, C 7, D 4, E 3, F 3, G 2 |
| Primary held-out split | 23 questions; not run until a single configuration is frozen |
| Extension set | `golden/crv_golden_extension.jsonl`, SHA-256 `1730faf9a9b81eee5c38d0c5cf838a71c8912e3737fbd655ac0da9e9cf1e6960`, 466 dev questions (A 415, B 28, D 1, F 20, G 2) |
| Release | `crv-v1` (145,855 chunks) |
| Code | Git revision and backend and pipeline image digests of each run |
| Models | Served model name, revision hash, precision and server flags for every model |

Record the inputs above in each run's evidence folder. Recompute the dataset hashes before a run;
a changed hash means a new baseline.

## Arms

| Arm | Search | Embeddings | Reranker | Generation | Kind |
|-----|--------|------------|----------|------------|------|
| AZ | Azure AI Search | Azure TE3 | Azure semantic ranker | Azure | Baseline (existing reports) |
| EDGE-M3 | Edge PostgreSQL, `m3` column | Local BGE-M3 | None | Not used | Retrieval |
| EDGE-M3RR | Edge PostgreSQL, `m3` column | Local BGE-M3 | Local bge-reranker-v2-m3 | Not used | Retrieval |
| EDGE-LOCAL | Best of the retrieval arms | Local | As chosen | NVIDIA API in phase 1, local System 1 and 2 in phase 2 | End to end |

No edge arm calls Azure. Arm labels use letters, digits and hyphens only, as the evaluation CLI
requires.

> [!NOTE]
> The EDGE-TE3 parity arm (Azure TE3 query vectors against the restored `te3` column) was dropped on
> 2026-10-29. It needs the DGX to reach the private Azure OpenAI endpoint, which requires a
> site-to-site VPN. Its purpose is already covered: the restore matched the source exactly (P6), and
> searches over the restored Azure vectors on the DGX found 100% of the exact nearest chunks
> (`retrieval-verify recall --model te3`).

The retrieval CLI arms `A0`, `A1`, `LEX` and `CEIL` call search only, never a chat model, so
retrieval comparisons are not confounded by generation
([retrieval.py](../src/backend/crv_agent/evaluation/retrieval.py)).

## Baseline artifacts

| Report | Holds |
|--------|-------|
| [golden-retrieval-baseline-2026-10-21.md](./evaluation/golden-retrieval-baseline-2026-10-21.md) | anchor@k, facts@50 and judge@10 per mode and category; judge gpt-5.4-mini; noise about 2 points between identical runs |
| [agent-retrieval-depth-2026-10-21.md](./evaluation/agent-retrieval-depth-2026-10-21.md) | Context depth arms behind the evidence-pack defaults |
| [agent-answers-2026-10-21.md](./evaluation/agent-answers-2026-10-21.md) | End-to-end answer gates and the first answer baseline |
| [PGVECTOR_RETRIEVAL_TEST_PLAN.md](./PGVECTOR_RETRIEVAL_TEST_PLAN.md) | Adoption gates and the Stage 1 AZ and PG configurations |
| Preflight historical baseline | Run `b1027214054` and the earlier PG versus AZ comparison ([preflight](./DGX_MIGRATION_PREFLIGHT.md)) |

Earlier AZ and PG runs were scored separately in their pods, so they were unpaired. Their contexts
went to the pod log and were not kept, so a fresh AZ export is needed. This kit scores all exported
contexts together with one judge configuration.

## Commands

### Retrieval contexts per arm

Run from the backend image on dgx-niz01 with the arm's settings (see
[edge.env.example](../infra/dgx/edge/edge.env.example)).

```bash
python -m crv_agent.evaluation.retrieval --golden golden/crv_golden.jsonl --split dev \
    --arm A0 --arm A1 --arm LEX --arm CEIL --label REPLACE_ARM_LABEL \
    --out /srv/crv-secure/eval/REPLACE_RUN/REPLACE_ARM_LABEL-contexts.jsonl --confirm-live
```

`--confirm-live` acknowledges model and search calls; for the edge arms those calls are local.
Export the AZ contexts from the AKS job of the same code revision; the retrieval arms are unchanged
since `4520b64`.

### Paired scoring

Score every arm's contexts in one invocation so all arms share the judge, its prompt and its
repeats:

```bash
python -m ingest_pipeline.tools.golden score --golden golden/crv_golden.jsonl \
    --contexts AZ-contexts.jsonl \
    --contexts EDGE-M3-contexts.jsonl --contexts EDGE-M3RR-contexts.jsonl \
    --reference AZ_A1 --reference AZ_CEIL --repeats 3 \
    --judge-deployment REPLACE_JUDGE --openai-endpoint REPLACE_JUDGE_ENDPOINT \
    --out /srv/crv-secure/eval/REPLACE_RUN/score.json
```

`--reference` names the labeled baseline arms for paired per-question deltas.

### ANN versus exact recall and plans

```bash
docker run --rm --network container:crv-edge-postgres-1 \
    -e TIZ_DB__DSN=postgresql://postgres@127.0.0.1:5432/ingestion-catalog \
    crv-pipeline:REPLACE_TAG retrieval-verify recall explain --model REPLACE_MODEL \
    --release crv-v1 --sample 50
```

Run it with `--model m3` after its HNSW index exists. The `te3` column passed the same check after
the restore.

### End-to-end answers

Run against the edge web origin on dgx-niz01 (the CLI requires HTTPS for remote targets, so run on
the host against loopback):

```bash
python -m crv_agent.evaluation.cli --base-url http://127.0.0.1:8080 \
    --dataset golden/crv_golden.jsonl --split dev --arm EDGE-LOCAL --judge --gate \
    --output /srv/crv-secure/eval/REPLACE_RUN/answers-report.json \
    --answers-out /srv/crv-secure/eval/REPLACE_RUN/answers.jsonl --confirm-live
```

The judge in this CLI uses `CRV_OPENAI_ENDPOINT` and `CRV_EVAL_JUDGE_DEPLOYMENT`. Compare against
the AZ answers of the same split, judged by the same judge.

## Thresholds

| Metric | Arm | Required result |
|--------|-----|-----------------|
| A1 judge@10 | EDGE-M3, EDGE-M3RR | No more than 0.03 below AZ; report per category |
| CEIL anchor@50 | EDGE-M3, EDGE-M3RR | No more than 0.02 below AZ |
| ANN versus exact recall@50 | Each vector column | At least 0.95 |
| Citation contract | EDGE-LOCAL | `citation_contract_valid` 1.0, unchanged source IDs, locators and pages |
| Answer gates | EDGE-LOCAL | The thresholds in [gates.py](../src/backend/crv_agent/evaluation/gates.py), with no regression against AZ on the same split |
| Latency | EDGE-LOCAL | The budget approved before the run |

Differences under about 3 points of judge@10 are within the measured judge noise; report them as
ties, not wins.

### Judge decision

The judge must be identical across every compared arm. Decided 2026-10-29: keep the Azure judge
(`gpt-5.4-mini`) as an offline measurement tool. Scoring runs on the operator laptop over the VPN;
the edge runtime never calls it. The exported contexts travel from dgx-niz01 to an encrypted laptop
folder for scoring and are deleted there afterwards.

Record the judge's model revision in the evidence folder.

## Latency protocol

Agree the budget before measuring. Placeholders to fill:

| Measure | Budget |
|---------|--------|
| First answer event p95 at concurrency 1 | REPLACE |
| Full answer p95 at concurrency 4 | REPLACE |
| Retrieval call p95 (embedding plus database plus rerank) | REPLACE |

* Measure cold (first query after a restart) and warm separately.
* Run concurrency 1, 2 and 4 with unseen questions, at least 20 per level.
* Report embedding, database, rerank and generation time separately, plus time to first token.
* Record host memory, GPU utilization and swap on both hosts during each level.

## Evidence layout

Keep evidence outside Git, on the encrypted volume:

```text
/srv/crv-secure/eval/<run-id>/
    inputs.json          dataset hashes, release, revision, image digests, model revisions
    <arm>-contexts.jsonl retrieval contexts (contain CRV text)
    score.json           paired scoring output
    verify-<model>.json  retrieval-verify output
    answers.jsonl        end-to-end answers (contain CRV text)
    latency.json         per-level timings and host metrics
    summary.md           filled results table below
```

Creating `/srv/crv-secure/eval` needs `sudo` (the volume root belongs to root; runbook step 2).
Until it exists, runs write to `/srv/crv-secure/bundles/eval/<run-id>/` on the same encrypted
volume; move them once the folder is created.

## Results template

| Arm | A1 judge@10 | CEIL anchor@50 | LEX anchor@10 | ANN recall@50 | Retrieval p95 | Pass |
|-----|-------------|----------------|---------------|---------------|---------------|------|
| AZ | | | | n/a | | Baseline |
| EDGE-M3 | | | | 0.9996 | | |
| EDGE-M3RR | | | | | | |

| Arm | Citation contract | Key-fact recall A | Key-fact recall B and C | Abstention | Full answer p95 | Pass |
|-----|-------------------|-------------------|-------------------------|------------|-----------------|------|
| AZ | | | | | | Baseline |
| EDGE-LOCAL | | | | | | |

## Progress 2026-10-29

| Step | Result | Evidence |
|------|--------|----------|
| EDGE-M3 contexts, dev split, arms A0, A1, LEX, CEIL | 212 records, no errors, retrieval p50 0.04 to 0.25 s | `bundles/eval/retrieval-20261029/EDGE-M3-contexts.jsonl` (SHA-256 `f6bb2489…`) |
| `retrieval-verify recall explain --model m3 --sample 50` | Recall@50 0.9996 (minimum 0.98), approximate p95 489 ms, plans use `chunk_m3_hnsw_idx` with no sequential scan | `bundles/eval/retrieval-20261029/verify-m3.json` |
| AZ contexts | Not stored by earlier runs; export needed from AKS | n/a |
| Paired scoring | Not run | n/a |
| Quote-fidelity probe, 21 dev questions, 50 frozen passages each | Hosted bolt, lightning and minimax plus local lightning: 68 to 97% of quotes exact as returned; 80 to 98% accepted after the anchoring repairs (`cc020ad`) | `bundles/eval/quote-probe-20261029/score-final/` |
| Planner intent, 53 dev questions | Research questions routed away from the worker: lightning 36 of 44, bolt 11 of 47; after the prompt change (`d743294`) 11 and 1 | `bundles/eval/quote-probe-20261029/plan-*.jsonl` |
| End to end, 10 dev questions (A, B, C, D, E, G), no judge | Before: 0 answered, 3 turn errors. bolt in every role on `206498d`: 3 answered, 3 more with verified claims, 0 turn errors, first answer p50 24 s and p95 71 s | `bundles/eval/e2e-20261029/EDGE-{OLD,B}-report.json` |

`bundles/eval` stands for `/srv/crv-secure/bundles/eval` on dgx-niz01. The images were
`crv-backend:edge-4520b64-edge1` and `crv-pipeline:edge-4520b64-edge1`; the retrieval code is
unchanged in later commits.

## Azure-independence test

Run after EDGE-LOCAL passes. The goal is to show the edge works with no route to Azure.

1. Confirm the edge configuration needs no Azure credential: no `CRV_SEARCH_ENDPOINT`,
   `CRV_OPENAI_ENDPOINT` or `CRV_SOURCES_ACCOUNT_URL`; `CRV_SEARCH_BACKEND=postgres`;
   `CRV_PG_EMBEDDER=openai`; `CRV_MODEL_AUTH` is `none` or `key`. The backend then never creates an
   Azure credential.
2. Block outbound traffic from both hosts except between them, the operator's management path and,
   in phase 1, `integrate.api.nvidia.com` (generation). For example, add an `nftables` output rule
   that drops everything but the private subnet and the NVIDIA API addresses, or route only those.
   Record the exact rule. In phase 2, drop the NVIDIA exception: the edge must then run offline.
3. Prove the block: `curl -sS --max-time 5 https://login.microsoftonline.com` and
   `curl -sS --max-time 5 https://management.azure.com` both fail from each host and from the
   backend container.
4. Restart the stack (`docker compose down` and `up`), then check `/api/ready` returns 200.
5. Ask five pinned questions through the web origin. Each must stream a plan, facets and a verified
   answer with citations.
6. Open a cited source PDF from the answer; the ranged request must succeed from the local volume.
7. Search the backend logs for any Azure host name; there must be none.
8. Remove the block and record the window, the rule and the results in the evidence folder.

The test fails if any step needs Azure. The judge is not part of the runtime and is not exercised
during the test.
