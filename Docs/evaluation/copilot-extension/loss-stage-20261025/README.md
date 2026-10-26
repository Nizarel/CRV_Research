---
title: Six-Case Loss-Stage Diagnostic And Candidate Results
description: Isolated Manadry traces, source-grounded calculator and extraction fixes, paired Copilot SDK judging, and the failed promotion gate.
ms.date: 2026-10-25
---

## Decision

Do not promote the combined candidate or scale it to the 53-question development
regression yet. The diagnostic work identified real loss stages and the deterministic
recovery implementation works, but broad answer completeness remains inconsistent and
latency regressed. The original web deployment and frozen 466-answer baseline were not
changed. No original held-out questions were used.

## Experiment

Ran Q77, Q138, Q155, Q184, Q398 and Q481 against a trace-only baseline and three explicit
candidate revisions. Each runtime used the same pinned image, native configuration,
Search/KG releases and external Manadry chat endpoint. Only the declared Python overlays
differed. Each arm produced six schema-valid answers without generation errors.

The full SSE trace records question/request/thread correlation, planned facets,
worker-exposed source text, extraction rounds and validation outcomes, answer input
claims, drafts, deterministic verifier errors, model verdicts, pruning and final output.
Candidate round traces additionally retain actual candidate IDs and document scope.
Diagnostics were enabled only on isolated local-owner backends with loopback forwards,
no service or ingress and unique selectors unrelated to the web service.

| Arm | Run | Purpose |
| --- | --- | --- |
| TRACE_BASE | [trace-base-20261025-113847](../trace-base-20261025-113847/deployment-snapshot.json) | Installed baseline source with diagnostic-only instrumentation |
| TRACE_CAND | [trace-cand-20261025-115233](../trace-cand-20261025-115233/deployment-snapshot.json) | Calculator, bounded follow-up, binding guidance and primary-source context closure |
| TRACE_CAND2 | [trace-cand2-20261025-120223](../trace-cand2-20261025-120223/deployment-snapshot.json) | Document/page ordering and narrower optional method/context guidance |
| TRACE_CAND3 | [trace-cand3-20261025-122825](../trace-cand3-20261025-122825/deployment-snapshot.json) | Exact adjacent-fragment context, prior-claim provenance, distinct mineral quantities and gap-only context closure |

Each run directory contains answers, generation attempts/summary, stage records, full
trace JSONL, the deployment snapshot and archived module sources. Source-bearing traces
are evaluation artifacts, not suitable for unapproved public logging.

## Observed Loss Stages

* Q77: both halves of the intermediate-stock sentence were exposed. Extraction kept
  the 500-kt value as an unnamed stock, and verification correctly rejected that
  attribution. Pruning then removed the requested stock. The final candidate still
  omitted this stock; the adjacent-fragment mechanism alone did not resolve it.
* Q138: extraction returned participation facts but some numeric counts were inferred
  rather than printed in the quotes, so deterministic validation rejected them.
  Candidate instructions retained qualitative statements without invented counts.
  Conference participation improved, but themes and other reference elements remain
  incomplete. One-versus-two poster statements also need careful source scope.
* Q155: all four pycnometer readings were extracted and verified as numeric facts.
  The baseline verifier rejected uncited introductory/context sentences, and pruning
  removed useful attribution. Candidate one improved the table presentation; the final
  candidate's broader reference recall regressed. This is not an ingestion failure.
* Q184: baseline extraction assigned an AZAGNIE table to ALEPE because of inherited
  heading context. Candidate one extracted the correct numeric rows but attached
  unsupported method context to their claims, causing verification to reject all seven.
  Later candidates restored the requested table values, but qualifiers and other
  reference elements remain missing; the final judge also rejected one malformed result.
* Q398: initial retrieval exposed positive observations but not the contradictory
  conclusion. Follow-up retrieval exposed it. In candidate two it reached the ledger,
  but receipt/observation date and cross-report attribution errors led verification to
  reject claims, and pruning lost the conflict. Only candidate one retained the expected
  conflict flag in this single run. Later candidates did not establish stable handling.
* Q481: the baseline exposed the formula but rejected a model-generated arithmetic
  result and lacked a supported deterministic operation. All three candidates produced
  cited serial recovery and the observed-versus-computed difference. Reporting of the
  difference improved, but the strict reference interpretation remains partial.

These traces distinguish observed stage failures from the earlier direct-Search
hypotheses. They do not prove every one of the 449 problem cases has the same cause.

## Implemented Local Changes

1. Added `serial_recovery` and `percentage_point_difference` to the calculator, with
   cited numeric inputs, fraction/percentage normalization, bounds checks and exact
   Decimal arithmetic. A separate cited observed-recovery index can request both values
   during reconciliation. The derived claims retain calculation indices and provenance.
2. Changed the extraction stop condition so a useful partial claim does not discard
   a requested gap-directed rewrite. The existing two-round and shared budget limits
   remain. Budget-only task-group failures preserve earlier claims; unrelated failures
   still propagate. Repeated claims are deduplicated and resolved stale gaps are removed.
3. Added bounded primary-source context follow-up, restricted in the final candidate
   to cases with remaining gaps. Source text and exact-quote validation are unchanged.
4. Present passages in document/page order, supply only exact adjacent-page fragments
   with their original IDs, and retain prior claim quotes/IDs for follow-up extraction.
   Synthetic joins are not accepted as original source quotations.
5. Clarified local table/sample binding, component-specific quantities, cited optional
   method/scope fields and receipt versus observation dates. Calculation requests now
   prioritize the published formula/operands rather than an unasked full mass balance.
6. Added opt-in full trace output to the evaluator and diagnostic-only draft/verifier
   events. The isolated setup instruments the actual installed baseline modules and
   accepts explicit candidate overlays; generated Python is AST-checked before apply.

No question IDs, golden answer text or case-specific numeric constants were added to
agent logic. The stakeholder CSV was not modified. Business clarification of Q129,
Q304 and Q353 remains separate from implementation changes.

The verified calculator results are:

```text
22.76 + (100 - 22.76) * 87.52 / 100 = 90.360448 percent
94.68 - 90.360448 = 4.319552 percentage points
```

The arithmetic does not prove the physical cause of the difference.

## Paired Results

All four arms were judged with the official Copilot SDK, Haiku 5.5, rubric v3 and three
repeats. The final comparison reused 54 exact frozen attempts and requested only 18 new
ones. There are 72 unique `(question, arm, repeat)` attempts in total, of which 71 are
valid. The Q184/TRACE_CAND3 first attempt supplied an extra `explanations` field and was
rejected; provider usage and the rejected response were retained.

| Arm | Mean reference-element recall | Answer p50 | Answer p95 | Input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| TRACE_BASE | 25.32% | 28.91 s | 43.81 s | 305,067 | 18,968 |
| TRACE_CAND | 33.25% | 40.98 s | 60.05 s | 426,661 | 26,574 |
| TRACE_CAND2 | 35.94% | 47.88 s | 73.85 s | 380,917 | 27,388 |
| TRACE_CAND3 | 33.12% | 53.04 s | 61.03 s | 392,588 | 26,782 |

These are six-case development observations, not certified accuracy or causal estimates.
Worker model-router served-model mixes varied across runs, so code differences are not
the only source of variance. The fixed judge compares reference content but does not
independently inspect the source passages or certify all source-specific caveats.

The final candidate has five partial verdicts and one needs-review verdict. No arm has
a complete passing task under the current strict rubric. Task-success checks requiring
complete invocation telemetry remain unmeasured. The final candidate's median latency
is substantially worse than the trace baseline despite the deterministic calculation
improvement. Higher `answered` counts therefore do not constitute a promotion gate.

Use the [side-by-side CSV](comparison-final/comparisons.csv),
[paired outcome](outcome.json), [judge attempts](comparison-final/judge-attempts.jsonl)
and per-run traces to inspect individual omissions and verifier decisions.

## Verification And Cleanup

All 113 touched-slice unit tests passed, covering arithmetic, provenance, invalid units,
budget preservation, bounded rewrites, fragment exposure and diagnostic privacy gating.
Touched-file lint and strict type checks for nine production modules also passed.

[Runtime parity](runtime-parity.json) and the final candidate's source verification
confirm identical native settings/image and exact mounted-source hashes. An initial
PowerShell `kubectl` readback used IBM437 and displayed mojibake; an ASCII `sha256sum`
inside the pod proved deployed bytes were correct. UTF-8 readback fixed the archive
check, and the original console encoding was restored. No deployment was changed to
work around the readback issue.

[Cleanup](cleanup.json) records deletion of all four uniquely labeled diagnostic
deployments, their native/source ConfigMaps and model Secrets. Source archives and local
traces remain available; no credential value was exported. The original web deployment
remained ready. The 53-development, 23-held-out and full 466-answer candidate evaluations
were deliberately not started after the targeted gate failed.

## Next Iteration

Retain the calculator implementation as a separately testable candidate. Before any
production promotion, isolate the remaining repair work into smaller experiments:
preserve cross-page subject attribution, repair or recite optional context without
discarding correct numeric claims, and prevent source-supported conflict statements
from disappearing during pruning. Recheck the same targeted cases and the latency
budget before running the original development regression. Do not relax exact-source
validation, silently change golden expectations, or claim a passing production gate.