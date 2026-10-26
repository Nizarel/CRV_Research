---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 24,
  "task_success": 0,
  "task_success_rate": 0.0,
  "comparison_verdicts": {
    "partial": 21,
    "needs_review": 2,
    "fail": 1
  },
  "answer_statuses": {
    "answered": 19,
    "partial": 1,
    "insufficient_evidence": 4
  },
  "execution_statuses": {
    "completed": 24
  },
  "status_vs_comparison": {
    "answered:partial": 18,
    "answered:needs_review": 1,
    "partial:partial": 1,
    "insufficient_evidence:partial": 2,
    "insufficient_evidence:fail": 1,
    "insufficient_evidence:needs_review": 1
  },
  "judge_errors": 0,
  "fact_recall_measured": 24,
  "latency_measured": 24,
  "latency_ms_p50": 29992.48980000266,
  "latency_ms_p95": 57020.204000000376,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 24,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.
