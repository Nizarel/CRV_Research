---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 2,
  "task_success": 1,
  "task_success_rate": 0.5,
  "comparison_verdicts": {
    "pass": 1,
    "partial": 1
  },
  "answer_statuses": {
    "answered": 1,
    "out_of_scope": 1
  },
  "execution_statuses": {
    "completed": 2
  },
  "status_vs_comparison": {
    "answered:pass": 1,
    "out_of_scope:partial": 1
  },
  "judge_errors": 0,
  "fact_recall_measured": 2,
  "latency_measured": 2,
  "latency_ms_p50": 23458.4893500014,
  "latency_ms_p95": 43210.86050000304,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 2,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.
