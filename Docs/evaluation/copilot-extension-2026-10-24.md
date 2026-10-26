---
title: Stakeholder-Grounded Copilot Evaluation
description: Business reference import, generated-answer logging, Copilot comparison, execution metrics and current run prerequisites.
ms.date: 2026-10-24
---

## Implementation Status

The stakeholder [CSV](../QA_REMINEX_EXTENSION.csv) imports as 466 questions, Q77-Q542,
with 1,585 frozen reference elements. Original questions and expected summaries are
preserved verbatim in [the extension dataset](../../golden/crv_golden_extension.jsonl).
The original Q1-Q76 benchmark and its 53 development / 23 held-out split are unchanged.

Source SHA-256: `c6126beb9e4b3fea2345acd742ab147b5930a51c4f31a626535e5039049624e8`.
There are no blank expected answers or duplicate question texts. Category and behavior
metadata are proposals, not stakeholder approvals. Review the
[mapping](../../golden/crv_extension_review.csv), particularly Q129 (PNN difference versus
ratio) and Q304 (four lots announced, three described). Do not change the business CSV
to make generated answers pass.

The default judge transport uses the installed GitHub Copilot CLI. A synthetic reference
of `42` versus `La valeur est 42.` produced a schema-valid passing verdict using the
explicit model `claude-haiku-4.5`; that invocation reported 0.33 premium requests.
This is an adapter smoke test, not domain calibration or a business-answer baseline.
The active judge was changed at the user's request to `claude-haiku-5.5`. Copilot
confirmed that model ID in its response; its preflight reported 1 premium request.
Keep historical 4.5 logs separate. Score saved answers again under a new run directory
and model-pinned manifest rather than resuming or mixing 4.5 judgments.
The Python SDK installation remains blocked by TLS handshakes to the PyPI file host.
The full run instead uses the official Node SDK, `@github/copilot-sdk@1.0.26`, through
the `sdk-node` transport. This is an SDK transport, not a CLI prompt fallback. A live
synthetic test passed, followed by six valid judgments on two actual Manadry answers
with three repeats and concurrency two. SDK usage events confirmed `claude-haiku-5.5`.

Private access is restored. The initial DNS failure was for an unrelated, stale
Kubernetes context; the CRV service is reachable using an isolated kubeconfig targeting
`aks-crv-southcentralus`. Seven deployed calibration answers have been captured in
`copilot-extension/calibration-20261024-v1/`. No DNS, VPN, deployment, model settings,
indexes or graph releases were changed. No production quality improvement is claimed.

## External Agent Model Gate

The user-approved run `manadry-20261024-190257` completed all 466 generations and
1,398 unique SDK judge attempts. Generation started at `2026-10-25T02:09:54Z` and
finished at `2026-10-25T04:47:43Z`. Its [configuration](copilot-extension/manadry-20261024-190257/evaluation-config.json)
freezes question/evaluator hashes and concurrency two for both phases. Three SDK
judgments were attempted for every answer. All 466 answers passed the wire schema;
there were no generation errors. Judge quality limitations are recorded below.

The existing `manadry-di-key` secret in `kv-awmc6icu2kueq` was accessed without
displaying or persisting its value. Harmless completion probes against Manadry
succeeded for `gpt-6.1-sol` (served `gpt-6.1-sol-2026-09-29`) and `model-router`
(served `gpt-5.6-luna-2026-07-09`). A catalogue listing alone was not treated as
proof of usable deployments.

The prepared local runtime supports `CRV_MODEL_ENDPOINT` and masked
`CRV_MODEL_API_KEY` for planner, worker, answer and verifier clients. Retrieval
embeddings retain the existing `CRV_OPENAI_ENDPOINT` and managed-identity credential.
The approved isolated deployment uses the exact pinned image and native retrieval
ConfigMap from the web backend, separate labels, no service or ingress, and only a
loopback port-forward. Its immutable overlay changes the deployed chat factory's
endpoint and credential expressions; it does not import unrelated newer local code.
The serving modules are under `/usr/local/lib/python3.12/site-packages/crv_agent`,
not the `/app` source tree. All four live role/schema probes passed; see the
[preflight](copilot-extension/manadry-20261024-190257/model-preflight.json) and
[runtime snapshot](copilot-extension/manadry-20261024-190257/deployment-snapshot.json).
The superseded first isolated attempt was removed. No web deployment, service,
ingress, retrieval endpoint, VPN or DNS configuration was changed by this evaluation.

The requested full-run judge is GitHub Copilot SDK with `claude-haiku-5.5`.
Earlier CLI calibration remains historical; it is not a substitute for that SDK
requirement. The Node SDK was installed without disabling TLS:

```powershell
npm install --prefix "$env:TEMP\crv-copilot-sdk-1.0.26" `
  --no-audit --no-fund --ignore-scripts @github/copilot-sdk@1.0.26
```

The approved run is invoked by `infra/kubernetes/research-agent/Invoke-BusinessEval.ps1`.
Generation attempts and answers are flushed incrementally, with judging scheduled
automatically after all 466 outcomes are captured. Failed generations remain in the
denominator. Judge failures retain any provider usage and rejected JSON for auditing.
The original held-out set is not part of this extension run.

## Completed Baseline

The [side-by-side CSV](copilot-extension/manadry-20261024-190257/comparison-haiku55/comparisons.csv)
contains every question, original business reference, generated answer, reported status,
comparison, and available metrics. The [summary](copilot-extension/manadry-20261024-190257/comparison-haiku55/summary.json)
and [judge attempts](copilot-extension/manadry-20261024-190257/comparison-haiku55/judge-attempts.jsonl)
preserve the full denominator and rejected responses. Usage events confirmed the
requested `claude-haiku-5.5` model throughout.

| Result | Questions |
| --- | ---: |
| Strict complete pass | 5 |
| Partial | 214 |
| Fail | 56 |
| Needs review | 179 |
| No valid judge response | 12 |
| Total | 466 |

The strict pass rate is 1.07%, not a certified answer-accuracy estimate. Aggregation
requires three valid, unanimous verdicts and every applicable mandatory check. Of the
179 review rows, 134 have at least one rejected attempt and 45 have none. Two rows
also carry pre-existing reference-review notes. Mean reference-element recall is
18.03% across 454 questions with at least one valid comparison; it does not verify
groundedness or source correctness.

There were 200 rejected judge attempts out of 1,398 (14.31%): 147 non-verbatim answer
excerpts, 16 non-verbatim reference excerpts, 19 missing or duplicated reference
indices, 11 missing required excerpts, 6 inconsistent passing verdicts, and 1 SDK-response
parsing failure. The last retained returned content and provider usage; it is not evidence
of a provider outage. These are not silently converted into answer failures or passes. The raw
baseline and its validators remain frozen; repairing the judge requires a separately
identified candidate run, not editing these results or weakening evidence checks.

Agent-reported statuses were 350 answered, 98 insufficient evidence, 12 partial and
6 out of scope. End-to-end answer latency was p50 36.98 seconds / p95 76.32 seconds.
Provider usage totalled 28,469,029 input and 1,900,976 output tokens. The 241,863
reasoning tokens are already a subset of output; cached input was zero. All 466 rows
have token and latency measurements. Tool traces, routed per-call cost allocation,
actual monetary charges and groundedness remain unmeasured. Two mandatory tool checks
are blocked by missing traces. No prices or invocation counts are fabricated.

The frozen image still contains the reproduced ordinary-out-of-scope hazardous-warning
defect. A local fix separates ordinary unavailability from hazardous-procedure refusals;
it is not a measured improvement to this baseline. Candidate generation, the original
development regression and final held-out checks still require a live backend.
The cluster connection ended after generation; local judging completed independently.
Do not restart the cluster solely to finish judging. Run-owned Kubernetes cleanup
remains necessary when the cluster is next available. Azure management-plane inspection
confirmed `aks-crv-southcentralus` is `Stopped`; it has not been restarted for cleanup.

### Indexed Quote Candidate

Q101 demonstrates the quote-contract defect: the v1 judge removed Markdown markers,
joined list items and changed apostrophes when quoting an otherwise relevant answer.
The exact-substring validator correctly rejected that fabricated quote formatting.

The prepared v2 SDK contract returns inclusive 1-based answer-line ranges rather than
asking the model to retype excerpts. The evaluator reconstructs the original contiguous
substring with Markdown, Unicode and line endings intact, and resolves each reference
directly from its frozen fact index. Existing semantic, index-coverage, evidence and
pass-consistency checks still apply; no fuzzy matching or reference edits were added.
The rubric version changed so these responses cannot resume or overwrite v1 results.

The separate [candidate manifest](copilot-extension/manadry-20261024-190257/quote-contract-v2-smoke/run-metadata.json)
pins `stakeholder-answer-v2`, one repeat, and Q101/Q279 only. Its
[summary](copilot-extension/manadry-20261024-190257/quote-contract-v2-smoke/summary.json)
records two valid SDK judgments with no errors: Q101 passed and Q279 remained partial.
This is a smoke test of the quote contract, not a full replacement benchmark, calibrated
accuracy estimate or improvement in generated agent answers.

Validation passed: 33 focused import/comparison tests, lint, strict type checks for both
judge modules, and four local backend regression cases for out-of-scope handling and
external-chat isolation. The live smoke consumed two new Copilot judgments and made no
agent or cluster calls. A full v2 rejudge would incur additional Copilot usage; it has
not been started. Live agent retests, the original regression/held-out checks and deletion
of the isolated Kubernetes resources remain deferred while the cluster is stopped.

## Restarted Readiness And Calibration

On 2026-10-25, after the user restarted AKS and PostgreSQL, the production backend's
rollback-only readiness command passed: retrieval release `crv-v1`, 1,848 visible KG
documents, accessible memory tables, and the expected KG/DDL privilege denials. No
model calls, migration, grants or application deployment were performed by this check.

The completed run's isolated deployment, two ConfigMaps and model Secret were deleted
using its exact verified `app=crv-eval-manadry-20261024-190257` label. The web deployment,
shared configuration, Key Vault credential and local baseline artifacts were not deleted.

The user approved calibration of the repaired SDK judge on 24 saved answers. The
[selection](copilot-extension/calibration-20261025-v2/selection.json) contains all twelve
previously unscorable cases plus twelve quote, numerical, conflict, scope, ambiguous
reference and positive controls. The assistant's
[reference reviews](copilot-extension/calibration-20261025-v2/reference-review.jsonl)
were frozen before judging: 6 pass, 14 partial, 2 fail and 2 needs-review expectations.
These reviews are not stakeholder approval or independently certified domain truth.

The [evaluator freeze](copilot-extension/calibration-20261025-v2/evaluator-freeze.json)
records reviewer/source hashes. Only original questions, stakeholder references and
saved generated answers are sent to the SDK, not reviewer labels. The calibration
invokes Haiku 5.5 three times per case at concurrency two, for 72 planned judgments.
It does not regenerate agent answers or touch the original held-out questions.

The predeclared gate requires at least 98% valid attempts, a valid majority for every
case, at least 90% agreement with the frozen assistant reviews, zero material false
passes, and unchanged source hashes. The full 466-answer rejudge has not started.

### Completed V2 Calibration

All 72 attempts completed using the requested SDK/model. There were 71 valid responses
(98.61%) and a valid majority for every case, with no material false passes. The
[gate audit](copilot-extension/calibration-20261025-v2/gate-audit.json) records agreement
with the frozen assistant reviews on 19/24 raw majority verdicts (79.17%), below the
predeclared 90% threshold. Strict aggregate verdicts agree on 18/24 cases; this stricter
summary also incorporates failed attempts, disagreement and reference-review flags.
The promotion gate failed and was not relaxed after observing results.

The five raw-majority disagreements were Q101 and Q156 (administrative/source headers
treated as required findings), Q129 and Q304 (known reference-quality notes omitted
from the judge input although aggregation already honors them), and Q398 (report/date
metadata treated as a partial answer despite no requested lot findings). The rejected
Q281 attempt called an acknowledgment matched but supplied no answer line range.

### Prepared V3 And Targeted Diagnostic

Rubric v3 distinguishes administrative locators from required findings while retaining
material sample/lot identities, quantities, dates, units and attribution. It fails
metadata-only nonanswers unless abstention/premise correction is the expected behavior,
passes existing reference-quality notes to the judge, and requires an answer range for
matched acknowledgments. It does not send assistant labels or edit stakeholder answers.

The separate [targeted manifest](copilot-extension/calibration-20261025-v2/targeted-v3/run-metadata.json)
and [summary](copilot-extension/calibration-20261025-v2/targeted-v3/summary.json) cover
Q101, Q129, Q156, Q281, Q304 and Q398. All 18 responses were valid, unanimous and agreed
with the frozen expectations: three pass, two needs review and one fail. These selected
cases informed the correction, so this result is a development diagnostic, not unseen
calibration evidence or domain-accuracy certification. Local validation passed 34
focused tests, lint and strict type checks; no backend code was deployed.

The full 24-case v3 development retest completed under
`calibration-20261025-v2/full-v3`, reusing the 18 exact fingerprinted diagnostic attempts
and making only 54 new judgments. The original review labels and gate thresholds
remained frozen. The [development gate audit](copilot-extension/calibration-20261025-v2/full-v3/gate-audit.json)
passed every declared check: 72/72 valid responses, 24 valid majorities, agreement with
all 24 frozen assistant reviews, zero material false passes, unchanged source/reviewer
hashes and exact seed reuse. Usage events confirmed the official SDK and Haiku 5.5.

The [strict summary](copilot-extension/calibration-20261025-v2/full-v3/summary.json)
contains 6 pass, 14 partial, 2 needs review and 2 fail. These are outcomes for this
selected development sample, not an accuracy estimate for all 466 answers. Because
the rubric was revised after examining this same sample, the result is not independent
validation or stakeholder sign-off. An untouched spot-check or stakeholder review is
needed before claiming general judge reliability. Do not mix these outputs with the v1
baseline or the failed v2 calibration.

This stage consumed 144 distinct Copilot judgments: 72 for v2, 18 for the targeted v3
diagnostic and 54 additional judgments for the complete v3 development retest. The
18 copied seed attempts were not billed again. No additional 466-answer rejudge, live
agent regeneration or application deployment was started. Readiness and old-resource
cleanup are complete; baseline data and the original held-out set remain unchanged.

## Capture Generated Answers

Run from the repository root against an already-authorized HTTPS backend endpoint,
or a loopback forward to the existing service. Use a new run directory; the capture
command refuses to overwrite prior artifacts or the dataset. Replace the endpoint
below with the actual endpoint. Do not open or redeploy the ingress for this evaluation.

```powershell
.\src\backend\.venv\Scripts\python.exe -m crv_agent.evaluation.cli `
  --base-url https://YOUR-EXISTING-ENDPOINT `
  --dataset .\golden\crv_golden_extension.jsonl `
  --output .\Docs\evaluation\copilot-extension\RUN-ID\generation-summary.json `
  --answers-out .\Docs\evaluation\copilot-extension\RUN-ID\answers.jsonl `
  --attempts-out .\Docs\evaluation\copilot-extension\RUN-ID\generation-attempts.jsonl `
  --arm BASE --concurrency 1 --confirm-live
```

Start with a small calibration set using repeated `--question-id Q77` options. Send
only questions to the agent, never stakeholder expected answers. The original 53
development questions can be run separately for regression; reserve the 23 held-out
questions for the final configuration using `--split heldout --allow-heldout`.

## Compare Against Business References

```powershell
.\src\ingestion\.venv312\Scripts\python.exe -m ingest_pipeline.tools.golden compare-answers `
  --golden .\golden\crv_golden_extension.jsonl `
  --answers .\Docs\evaluation\copilot-extension\RUN-ID\answers.jsonl `
  --out-dir .\Docs\evaluation\copilot-extension\RUN-ID\comparison-haiku55 `
  --transport sdk-node --model claude-haiku-5.5 --repeats 3 --concurrency 2 --confirm-copilot
```

Use `--only Q77` for a selected calibration set, `--resume` to reuse completed judge
attempts with matching input hashes/model/rubric, and `--gate` for a nonzero exit when
any selected task is unsuccessful. `--no-judge` creates an inventory without contacting
Copilot; its rows are not successful tasks. Never interpret unjudged rows as measured
answer-quality failures. The judge has no available tools, uses a fresh temporary working
directory, disables repository instructions, and receives no reported answer-status
label to bias its content comparison. Explanations and excerpts are retained, not
reasoning events. Confirm organizational permission to send evaluation data to Copilot.

Each run produces a fingerprinted manifest, incremental judge-attempt JSONL, lossless
comparison JSONL, spreadsheet-safe side-by-side CSV, and JSON/Markdown summaries.
Answers are not truncated. Judge failure does not require generating the answer again.

## Interpret Metrics

| Field | Meaning |
| --- | --- |
| `answer_status` | Agent-reported answered, partial, insufficient_evidence, needs_clarification or out_of_scope; execution errors are separate |
| `comparison_verdict` | pass, partial, fail, needs_review, not_evaluated or error |
| `task_success` | Completed, schema-valid response meeting the reference/behavior and every applicable mandatory check; not synonymous with answered |
| `expected_tool_called` | Observed required-tool check; null when inapplicable or unmeasured, never a guessed False |
| `schema_valid` | Final agent response passes its actual Answer wire contract; judge schema validation is separate |
| `latency_ms` | High-resolution monotonic question-to-answer time, including setup but excluding offline judging |
| `tool_call_count` | Deduplicated observed tool invocations; unknown is null, not zero or the retrieval budget counter |
| Tokens | Provider-reported input/output, cached input, reasoning, total, plus raw usage and role usage; cache/reasoning subsets are not added twice |
| Costs | Estimated agent costs require actual per-model usage and a dated price book; actual charges and Copilot billing units remain separate |

A correct abstention, correction of a false premise, or appropriately qualified incomplete
answer may pass when the reference expects it. Extra information is not automatically
false, and missing source passages make groundedness unmeasured rather than verified.
Conflicts, decimal commas, units, dates, sites and laboratory/pilot/industrial scale matter.

The current API does not expose a complete tool invocation trace or routed-model token
allocation. `--telemetry` can supply **observed**, per-request JSONL with `request_id`,
`tool_trace_status`, `tool_invocations`, `model_usage_status` and `model_usage`. A tool
invocation has `id`, `name` and `outcome`. A model call has `id`, `model`,
`usage_source: provider`, `input_tokens`, `output_tokens` and `cached_input_tokens`.
Declare completeness only for unsampled, fully collected traces. Counters, citations
or a planned tool do not prove execution. Missing mandatory evidence blocks success.

For `--price-book`, supply JSON with `currency`, `effective_date`, `source_url` and
`models`. Each served-model entry contains `input_per_million`, `output_per_million`
and `cached_input_per_million`. Missing model rates/allocation yield partial or
unmeasured estimates. No prices are guessed, no role tokens are allocated by call
proportions, and Copilot premium-request usage is not billed using Azure token rates.

## Validation And Next Run

Focused tests cover lossless CSV import and provenance, legacy split/scoring/audit
compatibility, status-versus-success semantics, schema failures, timeouts/cancellation,
tool trace deduplication and missingness, cache-aware cost arithmetic, resume behavior,
output protection, CLI JSON parsing and subprocess cleanup. Run the golden evaluation
tests in the ingestion environment and `tests/unit/test_evaluation.py` in the backend
environment. CLI help is available for both commands.

The original SDK path passed 25 focused import/comparison tests, strict checking of its
two Python modules, and lint. The real smoke run captured two schema-valid answers
and six valid comparisons with no judge errors; both answers were graded partial.
The local out-of-scope wording fix is not in the frozen baseline image. The completed
baseline and separately versioned quote-contract candidate are described above.
Keep candidate agent retests distinct from the baseline. Delete only this run's isolated deployment,
ConfigMaps, credential Secret and owned loopback forward after results are secured.

## References

* [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
* [RAG correctness, relevance and groundedness](https://docs.langchain.com/langsmith/evaluate-rag-tutorial)
* [GitHub Copilot Python SDK](https://github.com/github/copilot-sdk/blob/v1.0.26/python/README.md)