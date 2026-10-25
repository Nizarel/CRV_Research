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
The optional SDK transport is pinned to `github-copilot-sdk==1.0.26`; installation was
blocked by TLS handshakes to the PyPI file host. The CLI transport needs no Python SDK.

Private access is restored. The initial DNS failure was for an unrelated, stale
Kubernetes context; the CRV service is reachable using an isolated kubeconfig targeting
`aks-crv-southcentralus`. Seven deployed calibration answers have been captured in
`copilot-extension/calibration-20261024-v1/`. No DNS, VPN, deployment, model settings,
indexes or graph releases were changed. No production quality improvement is claimed.

## External Agent Model Gate

The full 466-question evaluation is on hold until the agent uses
`https://manadry.cognitiveservices.azure.com/` for all chat roles. The existing
deployed web backend still uses the original CRV model resource; do not run the full
benchmark against it or relabel its answers as Manadry results.

The existing `manadry-di-key` secret in `kv-awmc6icu2kueq` was accessed without
displaying or persisting its value. Harmless completion probes against Manadry
succeeded for `gpt-6.1-sol` (served `gpt-6.1-sol-2026-09-29`) and `model-router`
(served `gpt-5.6-luna-2026-07-09`). A catalogue listing alone was not treated as
proof of usable deployments.

The prepared local runtime supports `CRV_MODEL_ENDPOINT` and masked
`CRV_MODEL_API_KEY` for planner, worker, answer and verifier clients. Retrieval
embeddings retain the existing `CRV_OPENAI_ENDPOINT` and managed-identity credential.
The override has passed focused tests, lint and type checks but has not been deployed.
Applying it to the existing service, or creating an isolated evaluation deployment,
requires approval under the earlier no-deployment constraint.

The requested full-run judge is GitHub Copilot SDK with `claude-haiku-5.5`.
Earlier CLI calibration remains historical; it is not a substitute for that SDK
requirement. The pinned Python SDK installation still fails with a verified-TLS
handshake error at the PyPI file host after VPN restoration. Do not disable TLS
verification, print the key, or start the full run before these prerequisites pass.

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
  --transport sdk --model claude-haiku-5.5 --repeats 3 --concurrency 1 --confirm-copilot
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

After private-network access is restored, calibrate the judge against manually reviewed
business cases before running all 466 questions. Investigate failures using captured
evidence, add reproducing tests, and prepare narrow local fixes. Deployment and a live
candidate retest remain separate approval steps.

## References

* [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
* [RAG correctness, relevance and groundedness](https://docs.langchain.com/langsmith/evaluate-rag-tutorial)
* [GitHub Copilot Python SDK](https://github.com/github/copilot-sdk/blob/v1.0.26/python/README.md)