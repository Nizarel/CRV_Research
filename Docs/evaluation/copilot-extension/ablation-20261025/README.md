---
title: Calculator, Continuation And Binding Ablations
description: Isolated Manadry ablations of the deterministic calculator, cross-page continuation binding and optional-binding repair, with a paired 53-question development regression judged by the Copilot SDK.
ms.date: 2026-10-25
---

## Decision

The clean paired development run shows no measurable recall gain overall. The combined
candidate (calculator, continuation binding, optional-binding repair and the four follow-up
fixes) scored 15.49% mean reference-element recall against 14.80% for a fresh unchanged
agent (+0.69 points; 6 wins, 8 losses, 39 ties), with lower latency. It was shipped for its
targeted fixes, Q65 recovery and Q398-style date repairs, not as a broad quality improvement.
See [Release](#release).

The earlier calculator-only gain (8.83% to 15.14%) was an artifact. Rate limits had removed
research facets from 25 of 53 base answers, and the clean base alone scores 14.80%.
Golden data were unchanged.

## Arms

Every arm used the same pinned backend image, native configuration, Search and KG releases
and external Manadry chat endpoint, with diagnostics on an isolated loopback-only backend.
Each overlay was generated from the installed baseline sources with guarded, unique-anchor
edits, so unrelated local worktree changes are excluded. Pod-side module hashes matched
every snapshot.

| Arm | Run | Change over the installed baseline |
| --- | --- | --- |
| TRACE_BASE | [trace-base-20261025-113847](../trace-base-20261025-113847/deployment-snapshot.json) | None (frozen six-case baseline) |
| DEV_BASE | [dev-base-20261025-133821](../dev-base-20261025-133821/deployment-snapshot.json) | None (paired development baseline) |
| CALC_ONLY | [calc-only-20261025-133030](../calc-only-20261025-133030/deployment-snapshot.json) | Serial recovery, percentage-point difference, comparison claim, calculation prompts |
| CONT | [cont-20261025-1430](../cont-20261025-1430/deployment-snapshot.json) | CALC_ONLY plus cross-page continuation citation, marker completion, whole context passages for the verifier |
| BIND | [bind-20261025-1430](../bind-20261025-1430/deployment-snapshot.json) | CONT plus verifier `unsupported_bindings`, which clears optional fields instead of dropping claims |
| V2 | [v2-20261025-1600](../v2-20261025-1600/deployment-snapshot.json) | BIND plus approximate inputs, observed-counterpart extraction and cite-together rules (mechanism check only) |
| DEV_BASE2 | [dev-base2-20261025-1502](../dev-base2-20261025-1502/deployment-snapshot.json) | None (clean paired development baseline) |
| DEV_COMB | [comb-20261025-1501](../comb-20261025-1501/deployment-snapshot.json) | V2 plus distinct-input and percentage-grade calculator validation |

## Six-Case Results

Judged with the official Copilot SDK, Haiku 5.5, rubric v3 and three repeats. Earlier
valid attempts were reused by fingerprint, so 72 attempts covered four arms with 72 valid.

| Arm | Mean reference-element recall | Answer p50 |
| --- | ---: | ---: |
| TRACE_BASE | 25.32% | 28.51 s |
| CALC_ONLY | 27.87% | 29.96 s |
| CONT | 16.83% | 32.60 s |
| BIND | 23.80% | 39.18 s |

Single runs swing by 15 to 33 points on questions an arm does not touch: Q138 scored 0% in
BIND and Q184 scored 0% in CONT. These means therefore cannot rank the arms. Use the
[four-arm comparison](../bind-20261025-1430/comparison-4arm/comparisons.csv) for
per-question detail.

## Development Regression

Both arms answered the 53 original development questions concurrently under identical load.
Four concurrent requests triggered Manadry `OpenAIRateLimitError` responses: 8 base and 4
candidate generations. Those questions were retried one at a time and merged, so all 106
answers completed. The originals are kept beside the `answers-merged.jsonl` files. Of 318
judge attempts, 317 were valid; Q46/DEV_BASE kept two valid repeats after one response
added a forbidden `explanations` field.

| Metric | DEV_BASE | DEV_CALC_ONLY |
| --- | ---: | ---: |
| Mean reference-element recall | 8.83% | 15.14% |
| Paired wins / losses / ties | | 14 / 7 / 32 |
| Pass / partial / needs review / fail | 1 / 24 / 8 / 20 | 2 / 22 / 9 / 20 |
| Answered / partial / insufficient | 18 / 10 / 25 | 14 / 13 / 26 |
| Answer p50, 41 jointly completed first-pass questions | 61.66 s | 60.40 s |
| Answer p95, same questions | 97.52 s | 123.74 s |
| Input tokens, same questions | 2,119,499 | 2,339,157 |

The largest changes (Q60 +100, Q28 +83, Q63 −50, Q16 −33 points) are on non-calculation
questions. The four calculation-intent questions stay at 0–7% recall in both arms. The p95
increase comes from a few slow outliers. Latencies are about twice the six-case runs
because of shared load and rate limiting. See the
[paired comparison](../calc-only-20261025-133030/dev53/comparison/comparisons.csv).

> [!WARNING]
> This comparison is contaminated. Under four concurrent generations, 25 of 53 merged
> answers in each arm silently lost one or more research facets to `OpenAIRateLimitError`,
> although the turn itself completed. The answers only show this as a coverage gap. On the
> 21 questions where neither arm lost a facet, recall was 8.96% for DEV_BASE and 17.72% for
> DEV_CALC_ONLY, with 6 wins, 3 losses and 12 ties. That direction is consistent but the
> sample is small. The six-case traces had no rate-limit facet failures. The evaluation
> runner now reports `research_complete` and `facet_errors` so this cannot pass unnoticed.

## Mechanism Findings

* Q481: the observed 94.68% is exposed. It sits in the same table row group, directly
  after the computed 90.36%, so baseline extraction omits it; retrieval is not the cause.
  CALC_ONLY reproduced 90.360448% and compared it with the printed 90.36%, not the observed
  value.
* Q77: the 500 KT value sits in a chunk that starts mid-sentence after the page break.
  Continuation binding cites the preceding chunk, and the verifier then links both halves.
  Baseline extraction still names the stock "not visible", and the composer cited each
  half in a separate sentence. V2's cite-together rule fixed the citation for Colline B.
* Q398: BIND reported receipt-date bindings as `unsupported_bindings`. Both observation
  claims survived with their date cleared; previously they were dropped.
* Q65: the calculator refused "environ 8 800 T" as a qualified input, although the
  reference expects an approximate recovery. V2 then exposed a pre-existing validation gap:
  `weighted_recovery` accepted input indices `[1, 1, 0, 0]`, reusing masses as grades.
* In 21 of the 25 base development answers marked insufficient, the composer itself
  declared insufficiency despite holding verified claims. The answers are cautious
  partials, for example when six sites or a phase label are not fully established.
  Verification failures (3) and empty ledgers (1) are minor causes.

## Follow-Up Local Changes

These source changes are tested locally and measured in the combined DEV_COMB arm.

1. Approximate (`~`) calculator inputs produce a result flagged `approximate`, and the
   derived claim carries `~`. Bound qualifiers (`<`, `<=`, `>`, `>=`) are still rejected.
2. Calculation inputs must be distinct claims, and `weighted_recovery` requires percentage
   grades and non-grade masses.
3. Calculation facets also extract the printed result and its printed observed counterpart.
4. A claim with several handles is cited with all of them together.
5. The evaluation runner reports `research_complete` and `facet_errors`.

All 196 backend unit tests, ruff and mypy pass on the local tree.

## Clean Paired Development Run

A fresh unchanged agent and the combined candidate answered the 53 development questions,
one request at a time per arm (two in flight). Answers that errored or lost a research facet
to `OpenAIRateLimitError` were retried until clean: 6 base and 2 candidate answers, one of
them twice. All 106 merged answers completed without facet loss. The
[four-arm comparison](../comb-20261025-1501/dev53/comparison-4arm/comparisons.csv) reused
the 317 valid earlier judgments; 2 of the 318 new attempts were rejected for a malformed
extra field, leaving two valid repeats for those rows.

| Metric | DEV_BASE2 (clean) | DEV_COMB (clean) |
| --- | ---: | ---: |
| Mean reference-element recall | 14.80% | 15.49% |
| Paired wins / losses / ties | | 6 / 8 / 39 |
| Pass / partial / needs review / fail | 2 / 20 / 10 / 21 | 3 / 26 / 9 / 15 |
| Answered / partial / insufficient | 28 / 1 / 24 | 31 / 0 / 22 |
| Answer p50 / p95, 53 first-pass questions | 47.58 s / 107.61 s | 45.99 s / 96.39 s |
| Input tokens, same questions | 4,376,901 | 4,534,596 |

The combined candidate has fewer outright fails and no latency regression. Q65 moved from
0% to 33% because the approximate-input calculator produced the expected approximate
recovery, about 84.47%. The largest losses (Q7, Q63, Q74, each 33 points) are answer-content
variance unrelated to the changed mechanisms, such as an omitted dissolution figure or a
different remediation lever.

Two independent base runs bound the noise. The contaminated DEV_BASE scored 8.83% and the
clean DEV_BASE2 14.80%, with 12 wins and 6 losses between them. Rate-limit facet loss alone
moved recall by about six points. Under clean conditions the earlier calculator-only answers
scored 15.14%, within noise of the clean base.

Anchor-based loss stages over the 154 anchored facts agree: the candidate answered 49 facts
against 46, and dropped fewer extracted facts before the answer (23 against 31). About a third
of expected facts are retrieved but never extracted in both arms. Extraction is the main
remaining loss, and about 40% of extraction facets hit the eight-claim cap
(`CRV_WORKER_MAX_CLAIMS=8`).

## Release Constraints

* The tested candidate is the deployed image plus the eight
  [combined overlay](../comb-20261025-1501/overlay) modules. Branch HEAD differs from the
  deployed image in six modules and adds an unshipped blob source feature that needs
  `azure-storage-blob`, so a release image must overlay the candidate modules onto the
  current image rather than build from HEAD.
* Production runs `CRV_REPAIR_MODE=prune` and calls its own Azure OpenAI resource, not
  Manadry. Binding repair also applies in prune mode. A rate-limited facet is still skipped
  silently in production; a bounded retry or a quota review is needed before relying on
  completeness under load.

## Release

On 2026-10-25 the combined candidate shipped as backend image `rd31`
(`crv-research-agent@sha256:ebde6c5a…`). It was built from the live `rd30` image plus the eight
tested modules, copied into both package locations and recompiled, with the original user and
command. A local run confirmed that every module imports and that exactly those eight modules
differ from `rd30`. Only the backend container of `crv-research-web` changed. After rollout the
pod was ready with no restarts, live module hashes matched the tested files, `crv_agent check`
reported no failures, and the service returned ready. No live chat request was sent, because
the API cannot delete test conversations from the shared history. The
[release record](../release-rd31/rollout.json), [Dockerfile](../release-rd31/Dockerfile) and
[image check](../release-rd31/verify-image.py) are archived. Roll back with
`kubectl -n crv rollout undo deployment/crv-research-web` or by setting the backend image to the
recorded `rd30` digest.

Raw source-bearing traces, stage records and duplicate paired-answer inputs from these runs
(42 files, 130.5 MB) were deleted after analysis; linked results, snapshots and overlays remain.
The parked extraction candidate was removed from the working tree; its full source stays
archived in [trace-cand3 module sources](../trace-cand3-20261025-122825/module-sources.json).

## Verification And Cleanup

Each run directory keeps its overlay sources, snapshot, runtime parity, answers, attempts,
summaries and comparisons; raw traces and stage records were removed as described above.
Each `cleanup.json` records deletion of the labelled deployment, ConfigMaps and model
Secret. No `crv-eval-*` resource remains in the cluster. Temporary build contexts, test
packages and helper scripts outside the repository were deleted, as were local Docker copies
of older agent tags that remain in the registry.

## Next Iteration

1. Ask one live question through the web interface to confirm rd31 against production's own
   model endpoint.
2. Add a bounded retry for rate-limited facets so answers never silently lose research.
3. Test `CRV_WORKER_MAX_CLAIMS` above 8 as a single-setting ablation, since extraction is
   the largest remaining loss stage.
4. Address composer-declared insufficiency with verified claims as a separate, measured
   status-calibration change.
5. Run every comparison at two requests in flight or fewer, and treat `research_complete`
   below 100% as an invalid run until retried.
6. Reconcile branch HEAD with the deployed image: HEAD carries six undeployed module changes
   and an unshipped blob source feature that need their own test and release.
