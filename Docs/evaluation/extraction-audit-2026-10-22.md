# Extraction audit: 2026-10-22 (dev split)

This audit explains why key facts that the research agent **shows** to its evidence extractor
never reach the claim ledger. The earlier funnel run (F3w) lost about two thirds of the exposed
facts at extraction, and raising the claim cap or batching the passages did not recover them.

* Run `aud1022`, arm `AUD`: agent image `rd19`, adopted configuration (worker `model-router` at
  low effort, planner/answer/verifier `gpt-6.1-sol`, prune repair), dev split, 53 questions.
* Diagnostics on: every facet records the passages it saw and every claim its extractor returned,
  with the claim's outcome and the validator's errors (`audit` in the answers JSONL). The exposed
  passage text was exported for this analysis only (`-ExportStages`).
* Judge: shared `gpt-5.4-mini` scorer, 3 repeats with a majority vote per fact, on three arms:
  the answer, the exposed passages (`AUD-EXPOSED`) and the claim ledger (`AUD-LEDGER`).
* Tool: `python -m ingest_pipeline.tools.golden audit` (see [Reproduce](#reproduce)).

## Funnel

| Stage (shared judge recall) | All | A | B | C | D | G |
|---|---|---|---|---|---|---|
| Exposed passages | 0.454 | 0.656 | 0.305 | 0.202 | 0.083 | 0.834 |
| Claim ledger | 0.196 | 0.256 | 0.105 | 0.112 | 0.083 | 0.5 |
| Answer | 0.165 | 0.243 | 0.045 | 0.092 | 0.083 | 0.5 |

Paired over 50 questions: exposed vs answer +0.289 [0.204, 0.381]; ledger vs answer +0.031
[-0.036, 0.096]. Per fact (181 measured): 101 never exposed, 45 lost at extraction, 12 lost at the
answer stage, 23 answered.

The answer stage loses little. Extraction loses about 0.26 of recall, and exposure (retrieval)
loses more: B and C facts are mostly never shown to any extractor.

## Claim validation

The 152 facets returned 922 claims. No claim was cut by the claim cap.

| Outcome or rejection cause | Claims | Validator right? |
|---|---|---|
| Accepted | 735 | |
| Qualifier printed as words ("environ", "autour de", "plus de", "au moins") | 32 | No |
| Stitched quote: several shown lines joined | 23 | Partly (each line is verbatim) |
| Verbatim quote, wrong or mis-copied passage ID | 22 | No |
| Quote differs only in whitespace or typography | 17 | No |
| `calculation_index` set on a plain worker value | 10 | No |
| Grouped thousands ("8 800 t") | 3 | No |
| Qualifier the quote does not state | 24 | Mostly yes |
| Value not printed in the quote | 28 | Yes |
| Quote not in any shown passage | 44 | Yes |

About 107 of 187 rejections were true claims rejected on form. The fix (see below) re-anchors each
worker claim on the exact shown text before validation and reads qualifier words and grouped
thousands. Replayed offline on this run, accepted claims rise from 735 to 814; the 108 remaining
rejections are genuine violations.

## Facts lost at extraction

Each of the 45 lost facts is located by its golden anchors (all anchors for short facts, at least
half for multi-value facts) in the exposed passages and in the returned claims.

| Bucket | Facts | Meaning |
|---|---|---|
| Extractor skip | 13 | Shown to a facet that asked for it (LLM check); no claim returned |
| Facet scope | 10 | Shown only to facets that did not ask for it |
| Judge noise | 7 | An accepted claim carries it; the ledger judge missed it |
| Validation | 1 | Only a rejected claim carries it |
| Unlocated | 14 | 9 facts have no anchors; 5 are paraphrased beyond anchor matching |

In 9 of the 13 skips, the passage holding the fact was among the first 5 of the 50 shown (Q1 "Chine
≈ 43 %" at rank 2, Q25 NaHS result at rank 2, Q32 2019 composite at rank 1). Position and volume
are not the cause; the extractor passes over prominent, relevant values. Several facet-scope facts
are context the golden answer adds beyond the question (Q1 smelting technologies, Q40 world
refined copper), so better facets can recover only part of them.

## Decision

The audit's pre-registered rule picks the largest actionable bucket:

1. **Extractor (skip).** Run worker arms with medium reasoning effort, and with `gpt-6.1-sol` as
   the worker, against `AUD`. This is the main recall lever inside extraction.
2. **Validator (form rejections).** Adopted as correctness hygiene: it recovers true claims (79 in
   this run) but almost no golden key facts, so it is not expected to move recall alone.
3. **Facet scope.** A planner change is a smaller, second lever (10 facts, partly unrecoverable).
4. **Exposure.** The largest loss overall (101 facts), concentrated in B and C, is retrieval. It is
   the target of the KG-2 pilot (studies and observations on Dossier Cu).

## Results of the extractor arms

All arms ran on agent image `rd20` (`rd19` plus the validator fix and the prune clean-up) with the
adopted configuration, and were scored together by the shared judge (3 repeats, paired against
`AN`, 50 measured questions).

| Arm | Worker | Recall | vs `AN` [95% CI] | A | p50 | p95 | First finding p50 |
|---|---|---|---|---|---|---|---|
| `AN` (control) | `model-router`, low effort | 0.152 | | 0.273 | 40.5 s | 97.9 s | 14.5 s |
| `WM` | `model-router`, medium effort | 0.173 | +0.020 [-0.044, 0.086] | 0.288 | 43.5 s | 105.0 s | 18.1 s |
| `WS` | `gpt-6.1-sol`, low effort | 0.186 | +0.034 [-0.034, 0.100] | 0.306 | 51.6 s | 111.0 s | 24.9 s |
| `P18` (previous deployment) | `model-router`, low effort | 0.148 | -0.004 [-0.064, 0.051] | 0.242 | 41.9 s | 98.6 s | 14.7 s |

The ordering matches the diagnosis (a stronger extractor recovers skipped facts, mostly in A), but
neither arm meets the pre-registered adoption rule (lower 95% bound above -0.01 for a gain of at
least +0.03) at this sample size, and `WS` costs 11 s at the median and 10 s to the first finding.
`rd20` is deployed with unchanged settings: it keeps recall (`AN` vs `P18`) and removes the
citation-only lines and bodiless headings that pruning used to leave in answers. Confirming a
stronger worker needs more questions than the dev split has; the held-out split is reserved for the
final configuration.

## Status label review

The 23 `insufficient_evidence` answers with verified claims in the deployed configuration were
pre-labelled by `gpt-5.4-mini`: 14 partly answered, 8 not answered, 1 answered. The label mostly
matches what users get (related evidence, main ask not established). Expert labels go in
[insufficient-review-2026-10-22.csv](insufficient-review-2026-10-22.csv) before any change.

## Reproduce

```powershell
./infra/kubernetes/research-agent/render-research-agent.ps1 -Job answer-eval `
  -AgentDigest <rd19> -PipelineDigest <scorer> -AnswerArm AUD -RepairMode prune `
  -Diagnostics -ExportStages -ScoreConcurrency 2 -Concurrency 2 -TurnSeconds 420 -Invoke
./infra/kubernetes/research-agent/fetch-eval-run.ps1 -RunId <run> -OutDir <dir>
python -m ingest_pipeline.tools.golden audit --golden golden/crv_golden.jsonl `
  --answers <dir>/answers.jsonl --stages <dir>/stages.jsonl --score <dir>/score.json --arm AUD `
  --judge-deployment gpt-5.4-mini --openai-endpoint <endpoint> --out-csv <dir>/lost-facts.csv
```

The stages file contains source text: keep it local and delete it after the audit.
