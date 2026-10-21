# Golden-set retrieval baseline: 2026-10-21

This report evaluates **retrieval only**. No agent exists yet. It covers the 76 golden questions in `Docs/QA_REMINEX.xlsx` (Sheet2), converted to [golden/crv_golden.jsonl](../../golden/crv_golden.jsonl) with categories A–G as defined in [AGENTIC_ARCHITECTURE.md](../AGENTIC_ARCHITECTURE.md) §1.1. Retrieval ran against Search release `crv-v1` (145,855 chunks) and KG release `crvkg-20261021-a40f0b27792a`. The tool is `python -m ingest_pipeline.tools.golden` (AKS job `golden-eval`).

## Metrics

- **anchor@k:** the share of a question's anchors found in the top *k* chunks. Anchors are numbers, codes, identifiers and names extracted deterministically from the expected answer; there are 710 anchors over 249 atomic facts.
- **facts@k:** the share of facts whose anchors are all present in the top *k* chunks.
- **judge@10:** the share of key facts that an LLM judge (gpt-5.4-mini, strict JSON) finds stated in the top 10 chunks. It is an upper bound on what an agent that reads 10 chunks could cite.
- The F questions (false premise or abstention) are not judged.

## Results (top 10 unless stated)

| Mode | anchor@10 | anchor@50 | facts@50 | judge@10 | p50 latency |
|---|---|---|---|---|---|
| bm25 | 0.403 | 0.648 | 0.411 | 0.191 | 0.09 s |
| hybrid (BM25 + vector) | 0.531 | 0.711 | 0.484 | 0.276 | 0.24 s |
| **semantic** (hybrid + semantic ranker) | **0.570** | 0.711 | 0.484 | **0.366** | 0.29 s |
| kg (semantic + KG-steered, RRF) | 0.576 | **0.734** | **0.517** | 0.345 | 0.79 s |

Judged recall at 10 by category, semantic mode, against the gates in [AGENTIC_ARCHITECTURE.md](../AGENTIC_ARCHITECTURE.md) §7:

| Category | n | judge@10 | Gate |
|---|---|---|---|
| A: fact lookup | 36 | 0.60 | ≥ 0.80 |
| B: synthesis / timeline | 14 | 0.13 | ≥ 0.60 |
| C: cross-site comparison | 10 | 0.07 | ≥ 0.60 |
| D: conflicts / versions | 6 | 0.28 | — |
| E: catalogue / provenance | 4 | 0.13 | — (answered from the KG, not chunks) |
| G: arithmetic | 2 | 0.00 | — (needs inputs, then a calculator) |

## Findings

1. **The semantic ranker is the right default.** It nearly doubles judged recall over BM25 (0.19 → 0.37) at under 0.3 s.
2. **Most misses are ranking, not recall.** For 25 questions the judge found nothing in the top 10, yet ≥ 50% of the anchors appear in the top 50. An agent that reads deeper (top 30–50 with reranking) or decomposes multi-part questions will recover much of this. Single-shot top-10 retrieval cannot pass B or C, as §1.1 of the architecture predicted.
3. **Periodic summaries crowd out the primary reports.**
   - 47% of the top-3 documents are monthly activity reports or executive summaries.
   - Only 40% are from `Dossier Cu`, where every golden answer is grounded.
   - The summaries mention the same studies with fewer details, so the facts aren't there.
4. **KG steering adds recall but not precision yet.** It gives the best anchor@50 (+0.023) and facts@50 (+0.033), but judged recall is flat (6 questions better, 9 worse, within ranker noise). Several factors limit it:
   - The graph resolved entities for 59 of 76 questions, but mostly projects (43) and organisations (26).
   - The 647 cover-page projects are broad and steer toward many documents.
   - Only 3 questions named a report code.
5. **Some answers are absent even at top 50**, notably Q19 (SOMIFER Amas 3 resource) and Q64/Q65 (the Bouskour synthesis figures). These need targeted retrieval of the specific report, through code, title or KG `find_documents`, rather than semantic similarity.

## Recommended next changes (ordered by expected gain)

1. **Demote periodic summaries** in ranking:
   - Use a Search scoring profile, or a filter-then-fallback on genre or document type.
   - Diversify results (at most 2 chunks per document in the top 10).
   - Mark monthly activity reports and executive summaries as `activity-report` in the KG genre and in the Search metadata.
2. **Agentic retrieval for B, C and D:**
   - Decompose the question into its parts.
   - Fan out per site, period or scale.
   - Read the top 30–50 chunks and rerank.
   - Keep an evidence ledger.
3. **Precise KG steering:**
   - Steer only on sites, report codes and *approved* organisations, as a boost rather than a restriction.
   - Use the KG for E questions (`document_lineage`, `find_documents`) and to date values for D questions.
   - Re-run after the expert gazetteer review.
4. **Golden-set hygiene:**
   - The report proposes expected sources for every question (`proposed_sources`, from the passages the judge cited).
   - Experts confirm them, so later runs can measure source-match.
5. **Re-run this baseline** after each change. The job takes about 2 minutes, plus a few cents of judge tokens.
