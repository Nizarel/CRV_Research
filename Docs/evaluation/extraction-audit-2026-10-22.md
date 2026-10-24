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

## KG-2 pilot: graph results on Dossier Cu

The pilot covers 10 documents from Dossier Cu (the first 10 by path). The worker identity extracted
them with `gpt-6.1-sol`. The output was reviewed (dry run `kx10230212`), and then the ops identity
imported exactly that file with `kg-import`. The import re-grounds every item on the current pages
and refuses any drift; it found none.

* **Extraction.** 24 studies and 237 observations from the model, 1 sample quote dropped by
  grounding, 2,728 typed table cells, 0 errors. Reviewing the table rules removed masses, operating
  hours and moisture read as yields or tonnage, sample codes read as elements ("RC 211 SB" as Sb),
  test conditions read as streams, and two-unit headers ("% ou ppm").
* **Import.** 89 entities, 89 assertions and 2,959 observations, all `candidate`.
* **Release.** `crvkg-20261023-187f938da63e`, activated for `dev` only. The web app stays pinned to
  `crvkg-20261021-adb13a21c7da`.

Agent image `rd21` (`rd20` plus the KG-guided facet) answered the dev split twice on the new
release, otherwise configured like `AN`. Shared judge, 3 repeats, paired against `K0`:

| Arm | Graph results | Recall | vs `K0` [95% CI] | A | B | C | p50 |
|---|---|---|---|---|---|---|---|
| `AN` (rd20, previous release) | off | 0.154 | +0.000 [-0.042, 0.037] | 0.276 | 0.058 | 0.041 | 40.5 s |
| `K0` | off | 0.155 | | 0.266 | 0.048 | 0.082 | 50.4 s |
| `K1` | on | 0.154 | -0.002 [-0.064, 0.057] | 0.264 | 0.025 | 0.061 | 42.8 s |

The guided facet ran on 9 questions (Q11, Q23, Q26, Q29, Q32, Q34, Q35, Q38, Q53). On those
questions, mean recall fell from 0.26 (`K0`) to 0.13 (`K1`), and no question gained:

* **Q38** (Cu/Fe selectivity at Ouansimi, B) ended as `insufficient_evidence` with no claims. The
  facet asked the worker to find 12 listed values (such as "24,20 %" and "17,42 %"), which it
  could not quote. The answer then failed source verification (chronology and attribution
  sentences), and pruning could not rescue it (see the rerun below).
* **Q11** (sea water at Tizert, A) kept its evidence but lost the headline result (5 points of Cu,
  15 of Ag) in the final answer.

**Decision.** `kg_results` stays off. Asking the worker to locate listed values does not recover the
exposure loss in B and C. It spends a facet on numbers that are mostly in tables, and the worker
cannot quote those compactly. Before extracting the rest of Dossier Cu, change how the graph is
used: let released studies and observations choose which documents the facets search, rather than
which values to quote. Then rerun `K0`/`K1` on the same release.

### Rerun: KG-2 chooses the documents

Agent image `rd22` replaces the value list. When graph results are on, released studies of the
resolved site or report code choose up to 8 documents (in period order, evenly spread). One extra
facet asks the user's own question and searches only those documents: the usual unrestricted
search leg is dropped for that facet. The planner's facets are unchanged.

`K0B` (graph off) and `K1B` (graph on) ran on the same release. One shared judge scored all five
arms, paired against `K0B`:

| Arm | Image | Graph results | Recall | vs `K0B` [95% CI] | A | B | C | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|
| `AN` | rd20 | off | 0.160 | +0.026 [-0.049, 0.105] | 0.281 | 0.078 | 0.020 | 40.5 s | 93.3 s |
| `K0` | rd21 | off | 0.190 | +0.056 [-0.030, 0.147] | 0.290 | 0.148 | 0.102 | 50.4 s | 89.3 s |
| `K1` (value list) | rd21 | on | 0.149 | +0.014 [-0.064, 0.090] | 0.254 | 0.025 | 0.061 | 42.8 s | 93.9 s |
| `K0B` | rd22 | off | 0.134 | | 0.233 | 0.025 | 0.031 | 46.2 s | 104.7 s |
| `K1B` (documents) | rd22 | on | 0.189 | +0.054 [-0.010, 0.122] | 0.365 | 0.033 | 0.051 | 43.0 s | 109.1 s |

With graph results off, `rd21` and `rd22` run the same code, so `K0` and `K0B` are two draws of
one configuration. They differ by 0.056, as much as `K1B` gains, so the +0.054 is within
run-to-run noise.

The extra facet ran on the same 9 questions. A diagnostics rerun of `K1B` on those questions
(`K1D`) shows what it did:

* **It searches the chosen documents.** It completed on 7 of 9 questions (50 passages each) and
  produced 25 accepted claims; 8 reached final answers (5 in Q53).
* **It is starved on large plans.** On Q32 and Q35 the planner already used 6 facets, and the
  extra facet ran out of budget before searching.
* **Gains are consistent on two B questions.** Q53 is `partial` in all three graph-on runs and
  `insufficient_evidence` in two of three graph-off runs; Q32 is `partial` in two of three graph-on
  runs and in no graph-off run.
* **Q38 collapses every time.** In all three graph-on runs (`K1`, `K1B`, `K1D`) the answer fails
  source verification and returns no claims; in all three graph-off runs it is `partial`. The
  verifier rejects chronology and attribution sentences in a longer timeline draft, pruning cannot
  produce a draft that passes, and the answer stage falls back to "did not pass source
  verification".

Gate (agreed before the rerun):

| Criterion | Result | Pass |
|---|---|---|
| `K1B` vs `K0B` lower 95% bound above -0.03 | -0.010 | yes |
| No steered question collapses where the control answered | Q38 collapses in every graph-on run | no |
| Chosen documents reach the evidence of most steered questions | searched in 7/9; an extracted document cited in 1/9 (`K1B`) and 3/9 (`K1D`) | partly |
| p50 latency up by 10 s at most | 43.0 s vs 46.2 s | yes |

**Decision.** The gate is not passed, so the rest of Dossier Cu is not extracted yet and
`kg_results` stays off. Document choice works mechanically and helps Q53 and Q32, unlike the value
list. Two fixes come before another rerun:

1. **Answer stage.** When verification fails after pruning, keep the claims the verifier accepted
   instead of dropping the whole answer. Q38's collapse comes from this fallback, not from retrieval.
2. **Facet budget.** Give the KG facet a reserved slot (run it first, or count it within the
   planner's six facets) so large plans do not starve it.

Both fixes are in agent image `rd23`: the answer stage lists the claims the verifier did not reject
when a named problem survives pruning (a blanket rejection still abstains), and the KG facet starts
first. The fallback is not specific to the graph: answers that end as "did not pass source
verification" occur in every arm (AN 4, K0 5, K1 4, K0B 7, K1B 7 of 53 dev questions), which also
explains part of the gap between the two controls `K0` and `K0B`.

### Rerun with the fixes (`rd23`)

`K0C` (graph off) and `K1C` (graph on) ran on `rd23` and the same release. One shared judge scored
AN, both `rd22` arms and both `rd23` arms, paired against `K0C`:

| Arm | Image | Graph results | Recall | vs `K0C` [95% CI] | A | B | C | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|
| `AN` | rd20 | off | 0.152 | +0.011 [-0.062, 0.082] | 0.273 | 0.058 | 0.020 | 40.5 s | 93.3 s |
| `K0B` | rd22 | off | 0.142 | +0.001 [-0.054, 0.052] | 0.226 | 0.045 | 0.031 | 46.2 s | 104.7 s |
| `K1B` | rd22 | on | 0.189 | +0.048 [-0.023, 0.122] | 0.365 | 0.033 | 0.051 | 43.0 s | 109.1 s |
| `K0C` | rd23 | off | 0.141 | | 0.214 | 0.077 | 0.020 | 46.6 s | 111.8 s |
| `K1C` | rd23 | on | 0.188 | +0.047 [-0.009, 0.110] | 0.311 | 0.058 | 0.051 | 40.9 s | 89.0 s |

* **The fallback works.** In `K0C` 3 answers became findings lists instead of collapsing, and 4 in
  `K1C`, including Q38 (12 claims in `K1C`, 0 in every earlier graph-on run). True collapses fell
  to 3 per arm.
* **The KG facet now always runs.** In a diagnostics rerun of `K1C` on the steered questions
  (`K1E`), it searched the chosen documents on 9 of 9 questions (none exhausted the budget) and 11
  of its 24 accepted claims reached final answers (Q53: 6, Q38: 3, Q34: 2).
* **The recall gain is not yet attributable to the graph.** The graph-off controls now agree
  (`K0B` vs `K0C` +0.001), but both graph-on arms gain about +0.047 mostly on the 41 questions the
  KG facet never touched (`K1C` vs `K0C`: +0.067 per question there, -0.044 on the 9 steered
  ones; Q7 and Q28 move by up to +1.00 without any KG step). With only 10 documents extracted, the
  steered set is too small to show an effect either way.

Gate for `rd23`:

| Criterion | Result | Pass |
|---|---|---|
| `K1C` vs `K0C` lower 95% bound above -0.03 | -0.009 | yes |
| No steered question loses all its claims where the control had some | none | yes |
| Chosen documents reach the evidence of most steered questions | searched in 9/9; claims in answers for 3/9 | yes |
| p50 latency up by 10 s at most | 40.9 s vs 46.6 s | yes |

**Decision.** The gate passes: extract the rest of Dossier Cu (dry run and review first, then the
reviewed import and a `dev` release), then rerun `K0`/`K1`. The larger steered set is what can show
whether document choice helps; `kg_results` stays off in production until then.

### Full Dossier Cu

* **Extraction** (dry run `kxa23173012`, worker identity): 181 documents, 0 errors, 304 studies,
  2,568 observations, 3 quotes dropped by grounding. The 33 documents without studies are weekly
  flashes, presentations, monitoring reports and feasibility-study chapters. Reviewing the table
  samples led to two more rule fixes (pipeline `agent-rd10`): a header stating a test condition
  ("D80 = 45 µm") no longer names the measured property, and English "Con" rows are concentrates.
* **Import** (`kg-import`, two parts under the ConfigMap size limit, server-side apply): no drift;
  779 entities and assertions, about 25,000 observations, all `candidate`. The 228 superseded
  records are the pilot's previous rows for the first 10 documents.
* **Release** `crvkg-20261023-8148ddf45463` (304 studies, 475 samples), activated for `dev` only.

`K0F` (graph off) and `K1F` (graph on) ran on `rd23` and this release. One shared judge scored them
with the `rd23` arms on the 10-document release, paired against `K0F`:

| Arm | Release | Graph results | Recall | vs `K0F` [95% CI] | A | B | C | D | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| `AN` | 10 docs (rd20) | off | 0.152 | -0.028 [-0.079, 0.013] | 0.273 | 0.058 | 0.020 | 0.083 | 40.5 s | 93.3 s |
| `K0C` | 10 docs | off | 0.156 | -0.024 [-0.072, 0.019] | 0.287 | 0.045 | 0.020 | 0.083 | 46.6 s | 111.8 s |
| `K1C` | 10 docs | on | 0.191 | +0.011 [-0.016, 0.038] | 0.311 | 0.058 | 0.071 | 0.167 | 40.9 s | 89.0 s |
| `K0F` | full | off | 0.180 | | 0.287 | 0.037 | 0.061 | 0.167 | 43.4 s | 88.3 s |
| `K1F` | full | on | 0.176 | -0.004 [-0.050, 0.041] | 0.301 | 0.046 | 0.020 | 0.083 | 49.1 s | 110.2 s |

* **More questions are steered.** The KG facet now runs on 14 questions (9 before); an extracted
  document is cited in 12 of them (11 without graph results), and no answer loses all its claims.
* **On those 14 questions recall rises** by +0.059 per question (Q26 +0.50, Q37 +0.33; none
  falls). On the other 36, which the graph never touches, it moves by -0.029: run-to-run noise of
  the same size as the gain, so the overall delta is -0.004.
* **B and C stay near zero in every arm** (B 0.04-0.06): document choice does not reach the facts
  these synthesis and comparison questions need.

Gate for the full release:

| Criterion | Result | Pass |
|---|---|---|
| `K1F` vs `K0F` lower 95% bound above -0.03 | -0.050 | no |
| No steered question loses all its claims | none | yes |
| Chosen documents reach the evidence of most steered questions | cited in 12/14 | yes |
| p50 latency up by 10 s at most | +5.7 s (p95 +22 s) | yes |

**Decision.** `kg_results` stays off. Document choice is safe and slightly positive where it acts,
but it is far from the design's adoption rule (+10 points on B/C/D/E, §8.6 of
[AGENTIC_ARCHITECTURE.md](../AGENTIC_ARCHITECTURE.md)), and single runs cannot resolve a +0.06
effect on 14 questions. The extraction, import path and release stay available for the next uses
of the graph (premise checks, observation lookups for the calculator, study enumeration).

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

KG-2 pilot: extract as the worker, review the printed JSONL, import it as ops (dry run first),
release for `dev`, then run the paired arms:

```powershell
./infra/kubernetes/pipeline/render-pilot.ps1 -Job kg-extract -KgLimit 10 -OpsClientId <ops> `
  -PipelineDigest <rd9> -RunId <run>
./infra/kubernetes/pipeline/render-pilot.ps1 -Job kg-import -KgFile <reviewed.jsonl> -KgDryRun `
  -OpsClientId <ops> -PipelineDigest <rd9> -RunId <run>
./infra/kubernetes/pipeline/render-pilot.ps1 -Job kg-release -OpsClientId <ops> -PipelineDigest <rd9>
./infra/kubernetes/research-agent/render-research-agent.ps1 -Job answer-eval -AgentDigest <rd21> `
  -PipelineDigest <scorer> -AnswerArm K1 -KgResults -KgRelease <release> -RepairMode prune `
  -Concurrency 2 -TurnSeconds 420 -Invoke
```

Apply only the rendered Job (and, for `kg-import`, the `crv-kg-extract` ConfigMap): the other
rendered objects are shared.
