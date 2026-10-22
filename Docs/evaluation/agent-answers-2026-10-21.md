# Agent end-to-end answers: 2026-10-21 (dev split)

This report evaluates **verified answers** from the research backend: the full graph is planner → facet workers → reconcile/calculate → compose → deterministic and model verification. It follows the [retrieval-depth evaluation](agent-retrieval-depth-2026-10-21.md), which fixed per-facet exposure at 50 focused full chunks.

* Releases: Search `crv-v1`, KG `crvkg-20261021-adb13a21c7da`.
* Golden set: the `dev` split of [golden/crv_golden.jsonl](../../golden/crv_golden.jsonl), 53 questions. The held-out split was **not** run.
* All agent roles use `gpt-6.1-sol` (GlobalStandard, 100K TPM). The judges are `gpt-5.4-mini`:
  * the evaluator's per-question fact judge;
  * the shared golden `score` judge, run 3 times with a majority vote per fact.
* The AKS job `answer-eval` runs the backend on loopback in the same pod, under the agent's own identity, with no Service or ingress. Concurrency is 2 questions, with a 420 s turn deadline.

Gates are those of the [architecture](../AGENTIC_ARCHITECTURE.md) §7. Expected sources and numeric tolerances are still unreviewed, so `source_match` and `numeric_exact` are **unmeasured** and fail their gates by design.

## Results

| Metric | Run 1 (`rd9`) | Run 2 (`rd11`) | Gate |
|---|---|---|---|
| Turns completed | 0.906 (3 rate-limit, 2 budget) | **0.962** (2 budget) | — |
| Citation contract valid | 0.979 (47/48) | 0.961 (49/51) | 1.0 |
| Abstention correct (F, n=3) | 0.0 | **1.0** | 1.0 |
| Conflict flagged (D, n=4) | 0.0 | 0.5 | 0.8 |
| Calculator used (G, n=2) | 0.0 | 0.5 | 1.0 |
| Catalogue used (E, n=3) | 0.67 | **1.0** | 1.0 |
| Key-fact recall, evaluator judge: all | 0.212 | 0.252 | — |
| Key-fact recall, evaluator judge: A (n=24) | 0.340 | 0.319 | 0.8 |
| Key-fact recall, evaluator judge: B+C (n=17) | 0.085 | **0.232** | 0.6 |
| Key-fact recall, shared judge ×3: all | 0.178 | 0.179 | — |
| First verified answer p50 / p95 | 82 s / 191 s | 106 s / 270 s | p95 ≤ 25 s |
| Model calls / input tokens per question | 6.9 / 73K | 7.3 / 92K | — |

Run 2 by category, shared judge: A 0.292, B 0.029, C 0.092, D 0.083, E 0.0, G 0.334. Judge agreement across repetitions was 0.96.

In run 2, 27 answers are `insufficient_evidence` and 24 are `partial`. In nearly all of them, the answer reports what it found, with citations, and states what is missing.

**Run 3 (`rd12`) was interrupted.** It includes the two fixes in the next section. A scheduled environment stop shut down AKS and PostgreSQL at 08:05 UTC, after about 45 of 53 questions, and its pod-local results were lost. Those fixes are covered by unit tests but are not yet measured end to end.

## What the live runs changed

Each defect below was reproduced against live services, fixed, and covered by a regression test.

| Defect (observed) | Fix |
|---|---|
| Strict structured outputs rejected Pydantic's `Decimal` schema (regex lookaround, HTTP 400), so every compose and extraction call failed in-cluster | `Claim.value` publishes a plain number/string/null union; a validator normalizes decimal commas; ranges and words become `null` (qualitative claim) |
| One unparsable claim value or truncated JSON rejected a whole facet | Dedicated worker output budget; claim and quote limits; facet isolation (a failed facet becomes a gap; only all-failed turns error, so provider failure is never "no evidence") |
| The LangChain tool-loop worker resent full-chunk results on every call (about 160K input tokens per facet), exhausting the budget and the 100K TPM limit | Default worker is now bounded **retrieve-then-extract**: one structured extraction per query, one rewrite only if nothing was found, each passage sent once. The agent worker remains behind `CRV_WORKER_MODE=agent` |
| The composer re-typed all claims (`LengthFinishReasonError`) and mis-copied 43-character evidence IDs | The composer selects ledger claims **by number** and cites short `[Sn]` handles, rewritten deterministically to evidence IDs |
| The model verifier discarded whole well-supported answers over one overstated sentence | The verifier reports unsupported claims and sentences, and fails only on factual errors. The single repair drops rejected claims; a final failing draft is **pruned verbatim** (deletion only), not discarded |
| False-premise answers were marked `partial` | `insufficient` means the main requested fact or document is not established, or the premise is false |
| The planner asked for clarification and refused a reagent-dosage lookup as a hazardous `procedure` | Planner rules: clarify only when nothing is searchable; `procedure` only for hazardous-substance handling; derive or verify questions route to `calculation` |
| Conflicts were never surfaced (D 0/4) | The composer reports conflicts by claim number; they are kept only if the claims are selected and differ |
| **The composer did arithmetic itself** (Q65: (840 × 0.246) / (8800 × 0.0278) = 84.47 %) | **Number grounding:** every decimal or percentage in an answer must match a cited claim or a deterministic calculation. Otherwise the repair retries, then the sentence is pruned |
| Answer citations pointed to evidence not bound to a selected claim (Q34, Q49; the run 2 contract failures) | Contract: every marker must belong to a selected claim; orphaned sentences are pruned (`rd12`, not yet measured) |
| Research spent the answer budget (2 `BudgetExceeded` turns); the verifier received whole chunks | 60K tokens of headroom reserved for answering; the verifier receives ±800-character windows around each claim quote (`rd12`, not yet measured) |
| A judge miscount or provider error aborted the evaluation run | The judge is keyed by fact index with retries; failures leave a question unmeasured; failed turns count as misses |

## Interpretation

* **Reliability and behaviour gates are now mostly met or close.** Turns complete, abstention is correct, the catalogue path is used, and conflicts and calculations are partially surfaced. Every surviving citation is a verbatim, located quote. The two remaining contract failures are addressed in `rd12`.
* **Factual recall is the binding constraint.** About 0.18–0.25 of key facts appear in answers. The exposure upper bound for the same retrieval was 0.37 (CEIL, dev), so about half of the exposed facts are lost between exposure and answer. They are lost through extraction caps, verification strictness and facet queries that differ from the original question. The rest is never retrieved. B (synthesis) and C (cross-site comparison) stay far below their 0.6 gate, consistent with the retrieval findings: these need structured scientific results (KG-2), not deeper top-k.
* **Latency is far from the 25 s p95 target.** Each `gpt-6.1-sol` call takes about 8 s even for short outputs, and the pipeline is sequential (plan → extract → compose → verify, plus repair). Meeting the target needs faster models for extraction and verification, streaming partial verified answers, or both. This is a model-selection decision, not a code fix.
* The two judges disagree on B+C: 0.232 from the evaluator's single-shot judge versus about 0.06 from the shared three-repeat judge. Small-category numbers are descriptive only.

## Next steps

1. Re-run the dev split on `rd12` once the environment is running, to confirm a citation contract of 1.0 and no budget errors. Then run the held-out split **once**.
2. A model ablation for latency and cost, holding everything else fixed: extraction and verification on a faster deployment, with planning and composition on `gpt-6.1-sol`.
3. Recall: reduce the exposure-to-answer loss by measuring extraction recall separately (claims versus exposed facts), and test lifting the claim cap for synthesis facets.
4. Raise `gpt-6.1-sol` capacity (quota 250) for evaluation: three run 1 turns failed on the 100K TPM limit.
5. Expert confirmation of expected sources and numeric tolerances, so that `source_match` and `numeric_exact` can be measured.
6. KG-2 structured results for category C and the comparison questions.
