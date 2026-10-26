---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 24,
  "task_success": 2,
  "task_success_rate": 0.08333333333333333,
  "comparison_verdicts": {
    "partial": 16,
    "needs_review": 5,
    "fail": 1,
    "pass": 2
  },
  "answer_statuses": {
    "answered": 14,
    "insufficient_evidence": 8,
    "partial": 1,
    "out_of_scope": 1
  },
  "execution_statuses": {
    "completed": 24
  },
  "status_vs_comparison": {
    "answered:partial": 10,
    "answered:needs_review": 3,
    "insufficient_evidence:fail": 1,
    "partial:partial": 1,
    "out_of_scope:partial": 1,
    "insufficient_evidence:needs_review": 2,
    "insufficient_evidence:pass": 1,
    "insufficient_evidence:partial": 4,
    "answered:pass": 1
  },
  "judge_errors": 1,
  "fact_recall_measured": 24,
  "latency_measured": 24,
  "latency_ms_p50": 47861.668100000315,
  "latency_ms_p95": 68981.72390000036,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 24,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.
