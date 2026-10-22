# Agent retrieval depth: 2026-10-21

This report measures **how much evidence the research agent should expose per research facet**. It scores the exact text each configuration ("arm") would show a worker. It does **not** evaluate generated answers; end-to-end answer quality is the next step.

* Releases: Search `crv-v1`, KG `crvkg-20261021-adb13a21c7da`.
* Golden set: [golden/crv_golden.jsonl](../../golden/crv_golden.jsonl). Tuned on the `dev` split (53 questions, 50 judged), then confirmed **once** on `heldout` (23 questions, 22 judged). Category F (abstention) questions are not judged.
* Tools:
  * `python -m crv_agent.evaluation.retrieval` emits contexts.
  * `python -m ingest_pipeline.tools.golden score` applies the golden-eval anchors and judge.
  * Both run in AKS job `retrieval-eval`, rendered by [render-research-agent.ps1](../../infra/kubernetes/research-agent/render-research-agent.ps1).
* Images: agent `crv-research-agent@sha256:c3c4d05f492c9c5ccc4f1deb3dc861d28429c76c370c39168e0406395b307373`, scorer `crv-pipeline@sha256:cfa9da7a5b38da42c4f04f9bf16b3d14d5caa14f775e2600fd7e71b13976db0b`.
* Planner and reranker model: `gpt-6.1-sol`. Judge: `gpt-5.4-mini`, run 3 times per arm and question, with a majority vote per fact.

## Method

* **judge** is the share of a question's key facts that the judge finds stated in the exposed text, averaged over questions. **anchor** is the share of deterministic answer anchors (numbers, codes, names) present in that text.
* `truncate` arms are judged exactly like golden-eval, with passages cut at 1,800 characters. All other arms are split into passages of at most 1,800 characters, so the judge sees all exposed text.
* **Parity check:** A0 and A1 reproduced same-day golden-eval `semantic` and `semantic_focused` exactly (identical anchor and judge scores on the checked questions).
* **Decision rule:** paired per-question deltas with 95% bootstrap confidence intervals (10,000 resamples). Adopt an arm only if:
  * the lower bound beats the references, and
  * no category drops by more than 5 points, and
  * its cost is reported.

## Arms

| Arm | Exposure per facet |
|---|---|
| A0 | Semantic top 10, full chunks (golden-eval `semantic`) |
| A1 | Semantic top 50, focus rerank, top 10 full chunks (golden-eval `semantic_focused`) |
| P16 | Agent retrieval (semantic plus lexical leg, identifiers, focus), 16 × 1,200-character excerpts (previous default) |
| P12 / P24 / P16W2000 | P16 with 12 or 24 excerpts, or 2,000-character excerpts |
| CAP | P16 with excerpts anchored on semantic extractive captions |
| LLM | P16 after a listwise `gpt-6.1-sol` rerank of 50 previews |
| DEC | Planner decomposition into facets, P16 per facet, union |
| KGB | DEC plus the precise KG boost (Site, ReportCode, approved Organisation) |
| CEIL | Agent retrieval, all 50 candidates as full chunks |
| AAR | Azure AI Search agentic retrieval knowledge base, minimal effort, release filter enforced |

## Dev results (n = 53, judged 50)

| Arm | judge ALL | A (24) | B (10) | C (7) | D (4) | anchor | Characters | Model calls |
|---|---|---|---|---|---|---|---|---|
| A0 | 0.320 | 0.565 | 0.093 | 0.123 | 0.167 | 0.550 | 10.2k | 0 |
| A1 | 0.317 | 0.551 | 0.159 | 0.102 | 0.083 | 0.571 | 10.8k | 0 |
| P16 | 0.276 | 0.463 | 0.096 | 0.151 | 0.167 | 0.565 | 13.4k | 0 |
| P12 | 0.250 | 0.435 | 0.096 | 0.061 | 0.167 | 0.534 | 10.3k | 0 |
| P24 | 0.315 | 0.520 | 0.124 | 0.143 | 0.167 | 0.637 | 19.3k | 0 |
| P16W2000 | 0.312 | 0.530 | 0.121 | 0.143 | 0.167 | 0.578 | 16.4k | 0 |
| CAP | 0.272 | 0.463 | 0.096 | 0.123 | 0.167 | 0.565 | 13.4k | 0 |
| LLM | 0.326 | 0.535 | 0.112 | 0.192 | 0.167 | 0.603 | 12.5k | 1 (11k input tokens) |
| DEC | 0.348 | 0.524 | 0.175 | 0.253 | 0.083 | 0.660 | 34.8k | 1 |
| KGB | 0.342 | 0.540 | 0.130 | 0.240 | 0.083 | 0.651 | 32.8k | 1 |
| **CEIL** | **0.369** | **0.590** | **0.206** | 0.172 | 0.167 | **0.716** | 50.1k | 0 |
| AAR | 0.340 | 0.582 | 0.153 | 0.123 | 0.167 | 0.573 | 11.6k | 0 |

Retrieval-only latency at p50 is about 0.3 s. DEC and KGB take about 8 s and LLM about 12 s, because they add a model call. Judge agreement across the three repetitions was 0.88–0.94.

Paired deltas against A1, 95% CI:

| Arm | Delta | CI |
|---|---|---|
| CEIL | +0.051 | [0.016, 0.095] |
| DEC | +0.031 | [-0.037, 0.104] |
| KGB | +0.025 | [-0.047, 0.093] |
| AAR | +0.023 | [-0.007, 0.057] |
| LLM | +0.009 | [-0.034, 0.051] |
| P16 | -0.041 | [-0.094, 0.005] |
| CAP | -0.045 | [-0.095, -0.004] |
| P12 | -0.067 | [-0.126, -0.018] |

Against P16, CEIL scores +0.093 [0.041, 0.152].

## Held-out confirmation (run once; n = 23, judged 22)

| Arm | judge ALL | A (12) | B (4) | C (3) | D (2) | anchor |
|---|---|---|---|---|---|---|
| A0 | 0.424 | 0.639 | 0.167 | 0.0 | 0.5 | 0.617 |
| A1 | 0.439 | 0.611 | 0.209 | 0.0 | 0.5 | 0.612 |
| P16 | 0.379 | 0.528 | 0.125 | 0.0 | 0.5 | 0.569 |
| **CEIL** | **0.553** | **0.806** | **0.250** | 0.0 | 0.5 | **0.716** |

CEIL scores +0.114 [0.023, 0.227] against A1 and +0.174 [0.061, 0.311] against P16.

## Decisions

1. **The runtime default is now CEIL:** all 50 focused candidates exposed as full chunks (`CRV_EVIDENCE_PACK_SIZE=50`, `CRV_EVIDENCE_EXCERPT_CHARACTERS=8000`). It is the only arm that beats both references on dev with no category regression, and it is confirmed on held-out. It adds no model calls and no measurable retrieval latency.
2. **The excerpt packs (P12, P16, CAP) are retired as defaults.** Cutting a chunk around the lexical query terms lost facts stated elsewhere in the same short chunk. Caption anchoring did not help.
3. **The LLM rerank, decomposition, KG boost and agentic retrieval are not adopted.** None beat A1 on dev with a positive interval, and they add 8–12 s and model cost. Decomposition is numerically better for synthesis and comparison (B, C), but not significantly; re-test it end to end, where the worker's own rewrites already decompose.
4. **AAR is measured at minimal effort only.** LLM query planning (low or medium effort) is preview-only in REST `2026-04-01` and was not run.

## Limitations

* This measures **exposure**, an upper bound on what a worker could cite, not answers. With full-chunk exposure, the worker must find facts in about 50k characters; end-to-end recall, citation validity and latency need the answer evaluation.
* Token cost: one full-chunk search costs roughly 13–17k input tokens, resent on each later model call of that worker. The 300k turn budget may cut multi-facet questions with rewrites; facets beyond the budget report gaps.
* Small categories (C: 7 dev / 3 held-out; D, E, G: 4 or fewer) are descriptive only. C stays poor in every arm (held-out 0.0). Cross-site comparison needs structured scientific results (KG-2), not deeper top-k.
* Expected sources are still unconfirmed proposals, so source-based (qrels) metrics are not reported.
* A bug found by this run is fixed: periodic-summary demotion and report-code promotion now read `source_path` and `document_code`. They had read the content-addressed `source_uri`, which made the focus rerank inert in the agent.

## Operational notes

* The Search semantic ranker and agentic retrieval billing were switched to the Standard plan: on the Free plan, semantic queries fail once the monthly allowance is used.
* `gpt-5.4-mini` capacity was raised to 1,000 only during scoring and restored to 30 afterwards.
* The AAR knowledge source `crv-evidence-ks` and knowledge base `crv-agent-aar-kb` are evaluation objects. Delete them when no longer needed.
