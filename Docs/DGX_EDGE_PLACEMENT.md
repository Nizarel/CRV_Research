---
title: CRV Edge Placement on Two DGX Spark Hosts
description: Starting service placement, memory budgets, serving runtime, inter-host traffic, failure modes, and model compatibility for the CRV edge deployment on dgx-niz01 and dgx-niz02.
author: CRV Research
ms.date: 2026-10-29
ms.topic: concept
keywords:
  - dgx spark
  - edge
  - placement
  - vllm
  - bge-m3
estimated_reading_time: 12
---

## Scope and decisions

The CRV edge is one deployment spanning two DGX Spark hosts, dgx-niz01 and dgx-niz02. It must
eventually run without Azure. There is no Azure fallback in this phase, and the Azure deployment
stays unchanged.

| Decision | Value |
|----------|-------|
| Topology | One edge over two hosts; services are placed, not duplicated |
| Hosts | dgx-niz01 is spark-7bef (192.168.88.10, direct link 192.168.100.10); dgx-niz02 is spark-ce60 (192.168.88.8, direct link 192.168.100.11) |
| Retrieval data | Restored `ingestion-catalog` (PostgreSQL 16.15, AGE 1.6.0, pgvector 0.8.2) |
| Embeddings | BGE-M3 (1,024 dimensions), served locally, after re-embedding into `embedding_m3` |
| Reranker | bge-reranker-v2-m3 cross-encoder, served locally (optional at first) |
| Generation, phase 1 | NVIDIA API catalog models (hosted, OpenAI-compatible) with the operator's key (decided 2026-10-28) |
| Generation, phase 2 | The same models served locally (see [serving guidance](./DGX_SPARK_SERVING_OPTIMIZATION.md)) |
| Conversation memory | Fresh `crv-agent-memory` database on the edge; Azure conversations are not moved |
| Agent to database | Same host, private Docker network, password auth from a 0600 file, TLS optional on that network |
| Link between hosts | Not encrypted; private interfaces, host firewall and an API key only (decided 2026-10-28) |
| Source PDFs | Copied from the operator laptop with the same folder structure (decided 2026-10-28) |
| User identity | Single local owner now; Keycloak later ([identity design](./DGX_EDGE_IDENTITY.md)) |

All placement below is a starting point. Memory figures are estimates from parameter counts and
precision, not measurements. Confirm every choice with the [validation kit](./DGX_EDGE_VALIDATION.md).

## Phase 1: generation through the NVIDIA API

Chat generation starts on the hosted NVIDIA API (`https://integrate.api.nvidia.com/v1`). The
backend needs no code change: it uses `CRV_MODEL_ENDPOINT` with `CRV_MODEL_AUTH=key` and the key in
a 0600 file on the encrypted volume. Retrieval stays local: BGE-M3 is not offered by the API for
this key, and the hosted NVIDIA rerankers reached end of life (HTTP 410 on 2026-10-28).

> [!IMPORTANT]
> In phase 1 the prompts, which contain CRV evidence excerpts, leave the edge for the NVIDIA API.
> The edge no longer depends on Azure, but it is not yet offline. Record this data flow and its
> approval in the P0 data-classification decision of the [preflight](./DGX_MIGRATION_PREFLIGHT.md).

On 2026-10-28 each candidate ran the backend's model probe: one strict JSON-schema call per role
with that role's real schema (`QueryPlan`, `Extraction`, `ComposedAnswer`, `Verdict`). A second
test sent 20 real planner prompts to each configuration. Neither test measures answer quality.

| Model | Roles passing | Planner prompts valid | Seconds per call | Role |
|-------|---------------|-----------------------|------------------|------|
| `nvidia/nemotron-3.5-lightning-30b-a3b`, reasoning `none` | 4 of 4 | 20 of 20 | 1 to 2 | Planner, answer, verifier |
| `nvidia/nemotron-3.5-bolt-e4b` | 4 of 4 | 20 of 20 | 1 to 2 | Worker |
| `openai/gpt-oss-20b` | 4 of 4 | 11 to 14 of 20 at any effort | 3 to 62 | Rejected: frequently returns an empty message |
| `nvidia/nemotron-3.5-lightning-30b-a3b`, default reasoning | 2 of 4 | n/a | 22 to 45 | Reasoning used the whole 4,096-token output budget |
| `nvidia/nemotron-3-ultra-550b-a55b` | 4 of 4 | n/a | 2 to 156 | Too slow for interactive use |
| Nemotron Super and Nano, Gemma 3, Llama Nemotron 70B | 0 | n/a | n/a | Not enabled for this key (HTTP 404) |
| `deepseek-ai/deepseek-v4.1-flash` | 0 | n/a | n/a | Timed out after 120 seconds |

The API also returned HTTP 429 (rate limit) under 20 concurrent requests; the backend retries with
the server's `Retry-After`, but the limit caps parallel facets.

> [!NOTE]
> End-to-end smoke chats on 2026-10-28 ended as `insufficient_evidence`. The 2026-10-29 diagnosis
> ([validation progress](./DGX_EDGE_VALIDATION.md#progress-2026-10-29)) found that quotation
> fidelity was not the main cause: the planner, the composer and hosted latency were.

### Diagnosis and configuration, 2026-10-29

A quote-fidelity probe (`python -m crv_agent.evaluation.quote_probe`) froze the passages production
shows for 21 golden dev questions and sent them to each worker candidate through the production
extraction prompt. Most quotations came back exact (68 to 97% as returned). After two anchoring
repairs (an unprinted qualifier is cleared, and literal JSON escapes are decoded), 80 to 98% of
claims passed validation. The remaining rejections are real errors (changed words or numbers,
spliced table rows), which the guard must keep rejecting.

| Finding | Evidence | Change |
|---------|----------|--------|
| The planner routed research questions away from the worker | lightning with reasoning off sent 36 of 44 research questions to `catalogue` or `clarify` (local NVFP4: 42 of 47); bolt sent 11 of 47 | Planner prompt narrowed (`d743294`): 11 to 1 for bolt, 36 to 11 for lightning, 42 to 6 locally; planner is bolt |
| The composer returned drafts without claim numbers or citation markers | lightning drafts had empty `claim_numbers`, so the verifier saw no sources | Composer is bolt; a draft that only fails the citation contract falls back to the cited claim list (`206498d`) |
| Hosted lightning calls stalled past the turn budget | 2 of 10 turns timed out at 120 s in the answer stage | No lightning role in phase 1 |
| bolt returned few claims per call | 1.6 accepted claims per question with one batch | `CRV_EXTRACT_BATCHES=4` (4.0 per question, 19 of 21 questions with a claim) |

The edge backend runs `crv-backend:edge-206498d` with bolt in every role since 2026-10-29. On 10 dev
questions it gave 3 verified answers and 3 more with verified claims, with no turn errors (before:
0 answered, the worker reached in 1 of 10 questions). The previous settings are in
`/srv/crv-secure/secrets/edge.env.bak-20261029`.

The edge sends no generation to dgx-niz02 in phase 1; it can also host the backups.

## Phase 2 start: local generation on dgx-niz02

Since 2026-10-29 dgx-niz02 serves `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (revision
`cc84af2f`) as `nemotron-3.5-lightning` from container `crv-system2` (`nvcr.io/nvidia/vllm:26.09-py3`,
vLLM 0.29.0). Nothing in the edge uses it yet.

| Setting | Value |
|---------|-------|
| Listen address | `192.168.100.11:8000`, the direct 200 Gb/s link whose only neighbor is dgx-niz01 (`192.168.100.10`); not reachable on Wi-Fi |
| Authentication | `VLLM_API_KEY` from `~/crv-models/secrets/vllm.env` (0600) on dgx-niz02; the same key in `/srv/crv-secure/secrets/niz02-vllm.key` on dgx-niz01 |
| Flags | `--max-model-len 65536 --gpu-memory-utilization 0.5 --max-num-seqs 8 --enable-prefix-caching --reasoning-parser nemotron_v3 --default-chat-template-kwargs '{"enable_thinking": false}'` |
| Memory | 17.9 GiB of weights; load takes about 145 s |

```bash
docker run -d --name crv-system2 --gpus all --ipc=host --restart no \
    -p 192.168.100.11:8000:8000 -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    --env-file "$HOME/crv-models/secrets/vllm.env" -e HF_HUB_OFFLINE=1 \
    nvcr.io/nvidia/vllm:26.09-py3 vllm serve nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 \
    --revision cc84af2fe71647d87f4486c064f320e1e7535243 --served-model-name nemotron-3.5-lightning \
    --max-model-len 65536 --gpu-memory-utilization 0.5 --max-num-seqs 8 --enable-prefix-caching \
    --reasoning-parser nemotron_v3 --default-chat-template-kwargs '{"enable_thinking": false}' \
    --host 0.0.0.0 --port 8000
```

Measured on the frozen probe passages, the local model extracts like the hosted one (80 to 88% of
claims accepted, a claim for 20 or 21 of 21 questions), but each extraction call took 43 to 58 s at
concurrency 4. vLLM reports no native FP4 kernels for GB10 in this image and falls back to
weight-only FP4 (Marlin). The next steps are a build with native GB10 FP4 kernels or FP8 weights,
a smaller worker model (`Qwen/Qwen3.5-9B` is cached on dgx-niz02), and the latency protocol of the
validation kit. With the narrowed prompt the local model planned 41 of 47 research questions for
the worker.

> [!IMPORTANT]
> The dgx-niz02 host firewall rule that admits only dgx-niz01 to port 8000 is not set yet (it needs
> `sudo`). Until then the bind address and the API key are the only controls. To point the edge at
> it, set `CRV_NIZ02_ADDRESS=192.168.100.11` for Compose (the default is the Wi-Fi address) and use
> `CRV_MODEL_ENDPOINT=http://dgx-niz02:8000/v1` with the key file above. The backend has one model
> endpoint for all roles, so phase 2 moves every role at once.

## Starting placement for phase 2 (local generation)

```text
                      LAN (restricted)
                            |
               +------------+-------------------------------+
               | dgx-niz01                                   |
  browser ---> | web (nginx :8080) -- backend (loopback 8000)|
               |        |                    |               |
               |  [database net, internal]   |               |
               |        |                    +--> crv-bge-m3 (vLLM, models net)
               |   PostgreSQL 16             +--> crv-reranker (vLLM, models net)
               |   /srv/crv-secure (LUKS2)   |               |
               +-----------------------------|---------------+
                                             | private link (LAN, unencrypted), API key
               +-----------------------------v---------------+
               | dgx-niz02                                   |
               |   System 1 model (vLLM :8000)               |
               |   System 2 model (vLLM :8001 or same server)|
               +---------------------------------------------+
```

dgx-niz01 holds every CRV byte: database files, password files, source PDFs and evaluation output,
all on the LUKS2 volume. It also runs the two small retrieval models, because every question calls
them and the calls stay on one host. dgx-niz02 holds only model weights, which are not CRV data,
and receives prompts that contain CRV excerpts in transit.

The Compose stack for dgx-niz01 is [infra/dgx/edge/compose.yaml](../infra/dgx/edge/compose.yaml).
Model servers run outside it and are configured by URL.

### Role mapping hypothesis

The backend has four model roles (`planner`, `worker`, `answer`, `verifier`), each calling strict
JSON-schema structured output. The worker runs most often: up to `facet_concurrency` (default 4)
facets in parallel, each with `extract_batches` calls.

| Role | Starting model | Reason to test |
|------|----------------|----------------|
| planner | System 2 | Decomposition quality drives every later step |
| worker | System 1 | Highest call volume; quote extraction must stay exact |
| answer | System 2 | Composition and citation discipline |
| verifier | System 1 | Fast check; escalate to System 2 if verification quality drops |

Treat the table as a hypothesis. The end-to-end arm of the validation kit decides it.

### Alternative placement

| Option | dgx-niz01 | dgx-niz02 | Choose when |
|--------|-----------|-----------|-------------|
| A (starting) | DB, backend, web, BGE-M3, reranker | System 1 and System 2 | Generation contention on one host stays within the latency budget |
| B | As A, plus System 1 | System 2 only | Worker calls dominate latency and niz01 keeps memory headroom under load |
| C | As A | System 2 split across both hosts | Only if System 2 does not fit one host; adds inter-host traffic per token |

Compare A and B with the same questions at concurrency 1, 2 and 4. Prefer the option with the lower
end-to-end p95 that keeps at least the headroom in the budget below. Option C needs a multi-node
vLLM setup and is out of scope until a model requires it.

## Memory budgets

Each host has 128 GB of unified memory shared by CPU and GPU (121 GB reported usable on
dgx-niz01). Weight sizes are parameter count multiplied by bytes per parameter.

### dgx-niz01 (option A)

| Component | Estimate | Basis |
|-----------|----------|-------|
| OS, Docker, page cache floor | 8 GB | Reserve |
| PostgreSQL shared buffers | 4 GB | `shared_buffers=4GB` in the Compose file |
| PostgreSQL working set in page cache | 10 GB | 6.53 GB database, plus about 0.6 GB of M3 vectors and their HNSW index |
| Maintenance (index builds, restore) | 4 GB | `maintenance_work_mem=4GB`, transient |
| Backend and web | 4 GB | Backend limit was 3 GiB in AKS |
| BGE-M3 server | 4 GB | 568M parameters at FP16 is about 1.1 GB, plus runtime and batch buffers |
| bge-reranker-v2-m3 server | 4 GB | Same size as BGE-M3; 50 passages per semantic query |
| Headroom | Remainder (about 80 GB) | Evaluation runner, backups, option B |

### dgx-niz02 (option A)

| Component | Estimate | Basis |
|-----------|----------|-------|
| OS and runtime | 8 GB | Reserve |
| System 1 weights (8B) | 8 GB at FP8, 4 to 5 GB at NVFP4 | Parameter count times precision |
| System 2 weights (32B) | 32 GB at FP8, 16 to 18 GB at NVFP4 | Parameter count times precision |
| KV cache, both models | 30 to 50 GB | Depends on context length and concurrency; size from measured prompts |
| Headroom | At least 15 GB | Activation spikes and fragmentation |

> [!WARNING]
> vLLM reserves a fraction of device memory at start (`--gpu-memory-utilization`, default 0.9).
> On unified memory that fraction applies to the whole 128 GB. Set it explicitly for every server
> so the sum per host stays below the budget, for example about 0.05 for each retrieval model on
> dgx-niz01.

## Serving runtime

Use vLLM for all three model types. NVIDIA publishes a
[vLLM playbook for DGX Spark](https://build.nvidia.com/spark/vllm), and the vLLM OpenAI-compatible
server provides every endpoint the backend and ingestion clients call:

| Need | vLLM endpoint | CRV client |
|------|---------------|------------|
| Chat with JSON-schema output | `/v1/chat/completions` | `ChatOpenAI` with `CRV_MODEL_ENDPOINT` |
| BGE-M3 query and corpus embeddings | `/v1/embeddings` | `CRV_PG_EMBEDDER=openai`, `retrieval-embed` |
| Cross-encoder rerank | `/v1/rerank` (Cohere and Jina compatible) | `CRV_PG_RERANKER=vllm` |

One runtime means one image to pin by digest, one upgrade path and one set of memory controls.
Text Embeddings Inference remains supported (`CRV_PG_RERANKER=tei`) if a verified ARM64 and GB10
image becomes preferable.

### Shortlist to confirm

| Item | Candidate | Confirm before use |
|------|-----------|--------------------|
| Runtime image | NVIDIA vLLM container for DGX Spark | Digest, ARM64 manifest, GB10 kernels, license |
| Embeddings | `BAAI/bge-m3` | Served dimension is 1,024; maximum input length covers the longest chunk |
| Reranker | `BAAI/bge-reranker-v2-m3` | Scores 50 passages within the rerank budget |
| System 1 | A 4B to 8B instruction model | Strict JSON-schema output, license, quantized quality |
| System 2 | A 24B to 32B model at FP8 or NVFP4 | Same, plus planning and composition quality |

Download weights only after the bundle transfer finishes, record each model's revision hash, and
store weights outside `/srv/crv-secure` (they are not CRV data).

## Model compatibility audit

These checks come from reading the backend code. Live confirmation uses
`crv-agent check --models`, which sends one strict structured-output call per role with that role's
real schema ([operations.py](../src/backend/crv_agent/runtime/operations.py)).

| Backend behavior | Where | Local server requirement |
|------------------|-------|--------------------------|
| `with_structured_output(..., method="json_schema", strict=True)` | [models.py](../src/backend/crv_agent/adapters/models.py), [extraction.py](../src/backend/crv_agent/orchestration/extraction.py), [judge.py](../src/backend/crv_agent/evaluation/judge.py) | `response_format` with `json_schema`; vLLM enforces the schema through its structured-output backend |
| Schemas `QueryPlan`, `Extraction`, `ComposedAnswer`, `Verdict` | [domain](../src/backend/crv_agent/domain/) | Every construct in each schema must be supported by the grammar backend; probe each one |
| `max_completion_tokens` (4,096; worker 8,192) | [infrastructure.py](../src/backend/crv_agent/runtime/infrastructure.py) | Context length covers prompt plus output; the worker sends up to 50 passages |
| `reasoning_effort` per role | [settings.py](../src/backend/crv_agent/runtime/settings.py) | Leave unset for local servers unless the model documents it |
| Token budget from provider usage | `turn_tokens` budget | The server must return `usage` on every response |
| Agent worker mode (`CRV_WORKER_MODE=agent`) | [workers.py](../src/backend/crv_agent/orchestration/workers.py) | Tool calling; keep the default `extract` mode until tool calls are verified |
| API key | `CRV_MODEL_AUTH=key`, `CRV_MODEL_API_KEY_FILE` | Start vLLM with `--api-key` when traffic crosses hosts |

The ingestion `retrieval-embed` command validates every returned vector: count, index, dimension,
finite values and no all-zero vector.

## Traffic between hosts

In phase 1 no CRV traffic crosses the hosts; generation goes to the NVIDIA API over HTTPS. In
phase 2 generation traffic crosses them: backend on dgx-niz01 to the System 1 and System 2 servers
on dgx-niz02. Requests carry CRV excerpts.

* The link is not encrypted (decided 2026-10-28). Keep it on the private LAN or a direct cable.
* Bind the dgx-niz02 servers to the private interface that faces dgx-niz01, never to all interfaces.
* Allow only dgx-niz01 in the dgx-niz02 host firewall for the model ports.
* Require an API key (`--api-key` on the server, a 0600 key file on dgx-niz01).

The retrieval model servers on dgx-niz01 run as containers named `crv-bge-m3` and `crv-reranker`
on the `crv-edge_models` Docker network, which the backend joins through the web container. The
backend addresses them by name. For the one-off corpus backfill, connect `crv-bge-m3` to the
internal `crv-edge_database` network as well, so the pipeline running beside PostgreSQL reaches it
([runbook](./DGX_EDGE_RUNBOOK.md)).

## Failure modes accepted in this phase

| Failure | Effect | Response |
|---------|--------|----------|
| NVIDIA API unreachable (phase 1) | No generation; health stays ready but chats fail | Restore internet egress; phase 2 removes the dependency |
| dgx-niz02 down (phase 2) | No generation; health stays ready but chats fail | Restore dgx-niz02; option B keeps System 1 roles alive |
| dgx-niz01 down | Whole edge down | Restore dgx-niz01 and unlock the LUKS volume |
| Reboot of dgx-niz01 | Stack stays down until the operator unlocks the volume | Intended: passphrase is never stored on the host |
| Retrieval model down | Semantic queries fail; lexical search still works | Restart the server; `/health/ready` checks the database, not the models |

There is no standby and no automatic failover. Backups, restore tests and monitoring belong to
gate P8 of the [preflight](./DGX_MIGRATION_PREFLIGHT.md). The candidate backup target is an
encrypted volume on dgx-niz02, plus an independent copy outside the two-host failure domain.

## Open questions

* Approval of the phase 1 data flow: CRV excerpts in prompts to the NVIDIA API.
* Phase 2 System 1 and System 2 models, precisions and licenses; native FP4 or FP8 serving on GB10.
* The dgx-niz02 firewall rule for the model port (needs `sudo`).
* The evaluation judge stays on Azure as an offline measurement tool (decided 2026-10-29).
* Backup target, retention, recovery point and recovery time objectives (P8).
