---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 7,
  "task_success": 0,
  "task_success_rate": 0.0,
  "comparison_verdicts": {
    "partial": 6,
    "needs_review": 1
  },
  "answer_statuses": {
    "out_of_scope": 1,
    "insufficient_evidence": 2,
    "answered": 4
  },
  "execution_statuses": {
    "completed": 7
  },
  "status_vs_comparison": {
    "out_of_scope:partial": 1,
    "insufficient_evidence:partial": 1,
    "answered:partial": 4,
    "insufficient_evidence:needs_review": 1
  },
  "judge_errors": 1,
  "fact_recall_measured": 7,
  "latency_measured": 7,
  "latency_ms_p50": 26729.850499999884,
  "latency_ms_p95": 44979.915800000526,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 7,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.
