---
title: Direct Azure AI Search Evidence Review
description: Read-only evidence retrieval for partial, failed and review-required CRV answers, with source-backed remediation priorities and trace limitations.
ms.date: 2026-10-25
---

## Scope

Queried Azure AI Search directly for all 449 requested baseline problem cases:
214 partial, 56 fail and 179 needs review. The five baseline passes and twelve
unscorable cases were outside this selection. Original stakeholder references,
answers and judgments were not changed or rescored.

Target: `gptkb-awmc6icu2kueq`, index `crv-evidence-v1`, release `crv-v1`.
The backend currently uses Azure Search with KG-guided retrieval. This audit bypasses
KG scoping and answer generation; it is not a replay of the full agent workflow.

For each case, captured original-question lexical search, original-question hybrid
search with the configured semantic ranker/vectorizer, and a separately labeled
reference-informed lookup. Each bulk probe requested ten full chunks with source
paths, pages, IDs, hashes, ranks and scores. Reference-informed probes are diagnostic
oracles, not fair retrieval or answer-quality scores, and must not be fed to the
production agent as though the expected answer were user input.

Azure MCP rejected the private endpoint with `AntiSSRFException`. After consulting
the troubleshooting guidance, the audit used the existing Azure SDK/workload identity
inside the backend environment. No network protections, credentials, roles, index
documents or application deployment were changed. No chat, answer-generation or
Copilot-judge calls were made. Normal Search semantic/vector query charges may apply;
monetary cost and embedding token allocation were not measured.

## Results And Artifacts

* 449 distinct cases and 1,347 bulk queries completed, with zero query errors.
* Saved 13,413 hits covering 7,019 unique passage IDs; no hit had the wrong release.
* Two original oracle probes were empty, not the original-question queries.
* Fifteen cases received deeper source review. The remaining 434 are retrieved and
  indexed for inspection, not automatically assigned a semantic root cause.
* Follow-up queries captured ten specific source documents. Two initial 50-chunk
  probes were explicitly capped; subsequent probes captured all 159 and 92 chunks.

The [bulk manifest](retrieval/manifest.json) and [summary](retrieval/summary.json)
preserve selection and query accounting. Full, untruncated source content is in
[search-results.jsonl](retrieval/search-results.jsonl), approximately 34.5 MB.
The spreadsheet-safe [case index](case-index.csv) contains every selected question,
reference, original answer, lookup ranks, citation overlap and review state.
[Structured findings](source-findings.jsonl) record the fifteen reviewed cases with
exact passage IDs, page numbers, confidence limits and recommended fixes.

The [mechanical triage summary](case-index-summary.json) shows:

| Observation | Cases |
| --- | ---: |
| Original cited passage appears in direct hybrid top ten | 390 |
| Reference includes an extracted report/procedure code | 154 |
| Direct hybrid top ten contains a matching-code passage | 141 |
| Matching code found only through reference-informed lookup | 6 |

These are not fact-recall scores. A report-code match or citation overlap does not
prove that a missing fact is supported, that the worker saw its text, or that the
same index content was exposed during the historical baseline run.

## Source-Reviewed Findings

| Cases | Indexed evidence | What to fix or investigate |
| --- | --- | --- |
| Q398 | `CR/AR/FM/RP/42/18`, pp. 2-4: positive lots 0920/1037, negative 1108/0978, dark 1037 cathode, contradictory conclusion. The observations are hybrid rank 2 and already cited by the saved answer. | Preserve requested lot findings through extraction/verification and include the adjacent conclusion; return an attributed conflict instead of metadata only. |
| Q138 | `CR/GE/RP/98/17`, pp. 2-3 explicitly name the participant and two posters. The answer reasons from the revision table instead. | Prefer explicit body evidence over administrative attribution and search within the identified document before abstaining. |
| Q155 | `CR/AF/CO/RP/01/21`, p. 3 contains OREAS 293, the certified interval and all four readings: 2.61, 2.57, 2.60, 2.69. | Keep certificate/sample header context attached to every table row; trace why verification rejected an answer despite indexed measurements. |
| Q184 | `CR/AA/RP/091/18`, pp. 4-5 contain YAK R01 58/25/9/8 and ALEPE 43/33/24. Page 6 is AZAGNIE 1 despite inherited ALEPE heading context. | Bind claims to the local sample/table/page, not an inherited parent heading or a different sample's table. |
| Q481 | `CR/AF/IM/RP/27/16`, p. 3 contains all formula inputs and the reported actual recovery. | Add a cited-input serial-recovery operation and invoke it for calculation-verification requests. This operation shape is absent from the current calculator. |
| Q77 | `CR/AF/AG/PV/19/18`, pp. 2-4 split the intermediate-stock subject from the 500-kt value across chunks. Quartzite and malachite details are also present. | Join neighboring provenance-bearing context before extraction; do not lose a subject merely because its value continues on the next page. |
| Q111 | `CR/AR/TC/RP/001/06`, p. 4 has the complete requested foreword. Broad search also returns a similar 2007 statement from a different report. | Enforce requested period/report scope and retrieve the named section, rather than merging nearby-year statements. |
| Q123 | `CR/AR/PY/RP/011/05`, p. 4 explicitly gives the 60% quarter-summary result and fertilizer/acid conclusion. The answer cites it but substitutes detailed trial observations. | Retain reporting period, test mode and denominator; distinguish summary results from batch/heap values rather than replacing one with another. |
| Q141 | `CR/AR/RB/RP/23/16`, p. 3 contains all five metal-production objectives in a passage already cited for investment figures. | Extract complete requested economic rows/objectives; keep material streams and composition denominators distinct across reports. |
| Q192 | `CR/GE/RP/84/23`, p. 4 has separate quarter, annual and prior-year KPI columns. Nearby November tables contain plausible but different cumulative values. | Carry the column period into every claim and avoid mixing monthly, quarterly and annual cells. |
| Q129 | `CR/AR/EN/RP/18/18`, pp. 4-5 distinguish PNN difference and PN/PA ratio, with separate values and thresholds. | Keep typed quantities/units distinct; review the frozen reference's difference-or-ratio wording. |
| Q304 | `CR/AA/RP/134/15`, pp. 6-7 explicitly describe and tabulate lot 4, which the expected summary announces but does not describe. | Complete the reference through stakeholder review; do not penalize supported fourth-lot information merely because the summary omitted it. |
| Q171 | `CR/AR/FI/RP/03/11`, pp. 4-5 give trial-II recovery 89.78% versus goethite 87.68%, while the narrative recommends goethite under practical/grade considerations. | Compute the requested numerical optimum and separately attribute the practical recommendation and its criterion. |
| Q226 | `CR/RC/P/01.00` is indexed and contains distinct test-demand and project-management paths. | Preserve versioned procedure codes and select the correct process branch. The original empty oracle probe was an audit-parser defect, not missing ingestion. |
| Q353 | The requested consortium document is the direct hybrid top hit but its `document_code` field is null. Narrative and substance-table CAS assignments also disagree. | Review missing identifier metadata and reconcile source/CAS conflicts against authoritative safety records; do not reward literal agreement with an inconsistent summary. |

For Q481, independent Decimal arithmetic gives:

```text
22.76 + (100 - 22.76) * 87.52 / 100 = 90.360448 percent
94.68 - 90.360448 = 4.319552 percentage points
```

The numerical difference is verified. Its physical cause is not proved by arithmetic;
balance or sampling explanations remain hypotheses requiring evidence.

## Recommended Implementation Order

1. Capture one diagnostic trace per selected failure: exact Search candidates,
   KG scope, exposed spans, returned/capped claims, drafts and verifier rejections.
   The baseline does not retain enough of this state to distinguish extraction
   omission from composition or pruning with certainty.
2. Make extraction stopping depend on requested coverage, not merely any fresh
   claim. The current worker stops after fresh claims even if the result is only
   report metadata. Use at most a bounded, gap-directed follow-up within the
   identified document; do not solve this by increasing every token budget.
3. Retrieve neighboring paragraphs and table headers for an identified source.
   Preserve local sample, period, unit and row/column scope through verification.
   Use the existing batching and neighbor-table mechanisms before adding a new
   broad retrieval abstraction.
4. Extend the calculator contract and reconciler for serial-stage recovery with
   cited inputs, percentage bounds and preserved provenance. Do not ask the LLM
   to manufacture a numerical verification result.
5. Add conflict-aware answers and resolve reference/source-quality issues before
   interpreting strict benchmark scores as agent accuracy. Do not rewrite the
   stakeholder CSV to make outputs pass.
6. Test the targeted cases in an isolated candidate first, then development
   regression. Reserve the original held-out set for the final frozen setup.

These are proposed fixes, not deployed or measured improvements. The investigation
does not justify blanket reingestion: the decisive missing facts in these examples
are already present in the current released Search index.

## Diagnostic Corrections And Validation

The first report-code parser shortened dotted procedure identifiers. It was fixed
and regression-tested; derived metadata was corrected for Q225, Q226 and Q333.
Original queries and their raw results remain unchanged, including Q226's empty
truncated-code probe. Q353's empty code lookup accompanies null identifier metadata;
the document is present. The exact identifier/OCR cause still requires metadata review.

The bulk output was recovered after a Windows `kubectl cp` destination-path error;
only the copy was retried, not the 1,347 Search queries. Source finding IDs were
validated against the saved bulk and follow-up evidence. All 17 retrieval-evaluation
tests passed, along with focused lint and strict typing for the new audit helper.

The [transfer verification](transfer-verification.json) confirms all fourteen exported
evidence files matched their remote SHA-256 hashes before seventeen explicitly named
temporary audit paths were removed. The exact script used for the bulk run is archived
as [audit-executed.py.txt](audit-executed.py.txt). Local evidence, original baselines,
Search index data and the application deployment remain intact.